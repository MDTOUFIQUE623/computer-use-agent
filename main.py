import os
import sys
from dotenv import load_dotenv

# Ensure the 'src' folder is in our python path so graph imports work seamlessly.
src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.append(src_path)

from graph import app

def main():
    # Load environment variables
    load_dotenv()
    
    # Check if Gemini API key is configured
    if not os.getenv("GEMINI_API_KEY"):
        print("Error: GEMINI_API_KEY environment variable not found in .env or environment.")
        print("Please configure your API key in .env file before running.")
        sys.exit(1)
        
    print("=========================================")
    print("    Vision-Based AI Computer Control     ")
    print("=========================================")
    
    # Ask the user for a task
    task = input("\nEnter the task you want the agent to perform: ").strip()
    if not task:
        print("No task entered. Exiting...")
        return

    print(f"\nStarting task: '{task}'")
    print("Press Ctrl+C in this terminal, or move your mouse to any corner of the screen (Fail-safe) to stop.")
    
    # Initialize state
    initial_state = {
        "task": task,
        "screenshot": None,
        "action": None,
        "step": 0,
        "done": False,
        "history": []
    }
    
    # Run graph execution
    try:
        # We invoke the graph; because we print status updates in graph.py nodes,
        # we will see progress printed to the console as it executes.
        final_state = app.invoke(initial_state)
        
        print("\n=========================================")
        print("             RUN COMPLETE                ")
        print("=========================================")
        print(f"Final Step Count: {final_state['step']}")
        print("History of actions taken:")
        for idx, hist_entry in enumerate(final_state["history"], 1):
            print(f"  {idx}. {hist_entry}")
            
    except Exception as e:
        print(f"\nAn error occurred during execution: {e}")

if __name__ == "__main__":
    main()

