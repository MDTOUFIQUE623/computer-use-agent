"""
Tests for Brain — planning and routing.
Tests plan generation, step routing, and memory integration.
Does not execute any actual tools — just validates the plan structure.
"""

import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv()

from src.brain import Brain
from src.models import Plan, Step, ToolType, ActionType

brain = Brain()

# -----------------------------------------------------------------------
# Test 1 — simple file task
# -----------------------------------------------------------------------
print("Test 1: Simple file task")
plan = brain.plan_task(
    "move all PDF files from my Downloads folder to Documents"
)
assert plan is not None, "Plan is None — Gemini call failed"
assert isinstance(plan, Plan)
assert plan.total_steps > 0
assert len(plan.steps) > 0

print(f"  Task summary:  {plan.task_summary}")
print(f"  Total steps:   {plan.total_steps}")
print(f"  Complexity:    {plan.estimated_complexity}")
print(f"  Apps involved: {plan.apps_involved}")
print(f"  Notes:         {plan.notes}")
print("  Steps:")
for step in plan.steps:
    print(
        f"    {step.step_number}. [{step.tool.value}] "
        f"{step.action.value} → '{step.target}'"
    )
    print(f"       Expected: {step.expected_outcome}")
    if step.fallback_tool:
        print(f"       Fallback: {step.fallback_tool.value}")

# -----------------------------------------------------------------------
# Test 2 — browser research task
# -----------------------------------------------------------------------
print("\nTest 2: Browser research task")
plan2 = brain.plan_task(
    "search for the latest Python version and tell me what's new"
)
assert plan2 is not None
assert plan2.total_steps > 0

print(f"  Steps: {plan2.total_steps}")
for step in plan2.steps:
    print(
        f"    {step.step_number}. [{step.tool.value}] "
        f"{step.action.value} → '{step.target}'"
    )

# Verify browser tool is used
tools_used = [s.tool for s in plan2.steps]
assert ToolType.BROWSER in tools_used, (
    f"Expected browser tool, got: {[t.value for t in tools_used]}"
)
print("  Browser tool correctly selected ✓")

# -----------------------------------------------------------------------
# Test 3 — Spotify task
# -----------------------------------------------------------------------
print("\nTest 3: Spotify task")
plan3 = brain.plan_task("open Spotify and play music")
assert plan3 is not None

print(f"  Steps: {plan3.total_steps}")
for step in plan3.steps:
    print(
        f"    {step.step_number}. [{step.tool.value}] "
        f"{step.action.value} → '{step.target}'"
    )

# -----------------------------------------------------------------------
# Test 4 — routing
# -----------------------------------------------------------------------
print("\nTest 4: Step routing")
if plan.steps:
    first_step = plan.steps[0]
    routed_tool = brain.route_step(first_step)
    print(f"  Step 1 routes to: {routed_tool.value}")
    assert isinstance(routed_tool, ToolType)

    fallback = brain.route_fallback(first_step)
    print(f"  Fallback tool:    {fallback.value if fallback else 'none'}")

# -----------------------------------------------------------------------
# Test 5 — replan a failed step
# -----------------------------------------------------------------------
print("\nTest 5: Replan failed step")
if plan.steps:
    failed_step = plan.steps[0]
    new_step = brain.replan_step(
        original_step  = failed_step,
        failure_reason = "Tool reported ElementNotFound",
        attempt        = 1,
    )
    if new_step:
        print(f"  Replanned step:")
        print(f"    Tool:   {new_step.tool.value}")
        print(f"    Action: {new_step.action.value}")
        print(f"    Target: {new_step.target}")
        assert new_step.tool != failed_step.tool, (
            "Replan should use a different tool"
        )
        print("  Different tool selected ✓")
    else:
        print("  No alternative found (step may be impossible)")

# -----------------------------------------------------------------------
# Test 6 — memory integration
# -----------------------------------------------------------------------
print("\nTest 6: Memory integration")
import time
brain.record_success(
    task        = "move PDFs from Downloads to Documents",
    plan        = plan,
    duration_ms = 3500,
)
print("  Task pattern saved to memory ✓")

# Check hints are returned for similar task
from src.memory import get_memory_hints
hints = get_memory_hints("move files from Downloads folder")
print(f"  Memory hints for similar task: {'found' if hints else 'none'}")
if hints:
    print(f"  Preview: {hints[:150]}")

print("\nAll brain tests passed ✓")