import argparse
import sys
import time
import traceback
from pathlib import Path
 
# Match main.py's sys.path setup
src_path = str(Path(__file__).resolve().parent.parent / "src")
if src_path not in sys.path:
    sys.path.append(src_path)
 
from dotenv import load_dotenv
 
 
def _build_initial_state(task: str) -> dict:
    """Mirrors main.py's initial_state construction exactly."""
    from src.state import StateSlots
    return {
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
        "slots":              StateSlots(),
    }
 
 
def run_case(case, app, verbose: bool = True) -> dict:
    """
    Run a single TestCase. Returns a result dict with:
        name, category, status ("PASS"/"FAIL"/"ERROR"/"SKIPPED"),
        detail, duration_s
    Never raises — exceptions during setup/execution/teardown are caught
    and reported as status="ERROR" so one broken case doesn't abort the
    whole suite run.
    """
    result = {
        "name": case.name,
        "category": case.category,
        "status": None,
        "detail": "",
        "duration_s": 0.0,
    }
 
    if case.skip_reason:
        result["status"] = "SKIPPED"
        result["detail"] = case.skip_reason
        return result
 
    start = time.monotonic()
 
    try:
        if case.setup_fn:
            case.setup_fn()
    except Exception as e:
        result["status"] = "ERROR"
        result["detail"] = f"setup_fn raised: {e}"
        return result
 
    try:
        if verbose:
            print(f"\n{'-'*70}")
            print(f"RUNNING: {case.name}  [{case.category}]")
            print(f"  task: {case.task}")
            print(f"{'-'*70}")
 
        initial_state = _build_initial_state(case.task)
        final_state = app.invoke(initial_state, config={"recursion_limit": 100})
 
        passed, detail = case.assert_fn(final_state)
        result["status"] = "PASS" if passed else "FAIL"
        result["detail"] = detail
 
    except Exception as e:
        result["status"] = "ERROR"
        result["detail"] = f"{type(e).__name__}: {e}"
        if verbose:
            traceback.print_exc()
 
    finally:
        try:
            if case.teardown_fn:
                case.teardown_fn()
        except Exception as e:
            # Teardown failures shouldn't mask the actual test result,
            # but should be visible — append rather than overwrite.
            result["detail"] += f" [teardown also raised: {e}]"
 
    result["duration_s"] = round(time.monotonic() - start, 1)
 
    if verbose:
        symbol = {"PASS": "✓", "FAIL": "✗", "ERROR": "‼"}.get(result["status"], "?")
        print(f"\n{symbol} {result['status']}: {result['detail']} ({result['duration_s']}s)")
 
    return result
 
 
def print_report(results: list[dict]) -> None:
    print(f"\n\n{'='*70}")
    print("  REGRESSION SUITE REPORT")
    print(f"{'='*70}\n")
 
    by_category: dict[str, list[dict]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)
 
    totals = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIPPED": 0}
 
    for category in sorted(by_category.keys()):
        print(f"{category}")
        for r in by_category[category]:
            symbol = {"PASS": "✓", "FAIL": "✗", "ERROR": "‼", "SKIPPED": "—"}.get(r["status"], "?")
            totals[r["status"]] = totals.get(r["status"], 0) + 1
            dur = f"{r['duration_s']}s" if r["duration_s"] else ""
            print(f"  {symbol} {r['name']:35s} {dur:>8s}  {r['detail']}")
        print()
 
    total_run = totals["PASS"] + totals["FAIL"] + totals["ERROR"]
    print(f"{'='*70}")
    print(
        f"  {totals['PASS']} passed, {totals['FAIL']} failed, "
        f"{totals['ERROR']} errored, {totals['SKIPPED']} skipped "
        f"(of {total_run + totals['SKIPPED']} total)"
    )
    print(f"{'='*70}\n")
 
    if totals["FAIL"] > 0 or totals["ERROR"] > 0:
        print("NON-PASSING CASES:")
        for r in results:
            if r["status"] in ("FAIL", "ERROR"):
                print(f"  [{r['status']}] {r['name']}: {r['detail']}")
        print()
 
 
def main():
    parser = argparse.ArgumentParser(description="Run the v3 regression suite")
    parser.add_argument("--category", default=None, help="Only run cases whose category contains this substring")
    parser.add_argument("--name", default=None, help="Only run the case with this exact name")
    parser.add_argument("--include-skipped", action="store_true", help="Attempt cases marked skip_reason too")
    args = parser.parse_args()
 
    load_dotenv()
 
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(name)s | %(message)s")
 
    from harness.cases import get_cases
    from src.graph import app
 
    cases = get_cases(category=args.category)
    if args.name:
        cases = [c for c in cases if c.name == args.name]
        if not cases:
            print(f"No case named '{args.name}' found.")
            sys.exit(1)
 
    if args.include_skipped:
        for c in cases:
            c.skip_reason = None
 
    print(f"Running {len(cases)} case(s)...")
 
    results = []
    for case in cases:
        results.append(run_case(case, app))
 
    print_report(results)
 
    # Non-zero exit code if anything failed/errored, so this can be
    # wired into CI or a pre-commit check later if you want.
    if any(r["status"] in ("FAIL", "ERROR") for r in results):
        sys.exit(1)
 
 
if __name__ == "__main__":
    main()