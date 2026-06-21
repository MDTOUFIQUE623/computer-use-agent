import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
 
 
def _desktop_path() -> Path:
    """Mirrors the same Desktop-resolution logic used throughout the
    agent (brain.py's _build_planning_prompt, graph.py's resolve_path)."""
    home = os.path.expanduser("~")
    onedrive = Path(home) / "OneDrive" / "Desktop"
    regular  = Path(home) / "Desktop"
    return onedrive if onedrive.exists() else regular
 
 
DESKTOP = _desktop_path()
 
 
@dataclass
class TestCase:
    """
    One regression test.
 
    name:        short identifier, used in the report and as a key for
                 selective re-runs (e.g. running just one category)
    category:    groups related cases in the report output
    task:        the exact natural-language prompt sent to the planner,
                 identical to what a person would type at the
                 "What do you want me to do?" prompt
    assert_fn:   (final_state: dict) -> (passed: bool, detail: str)
                 Receives the raw dict LangGraph returns from app.invoke().
                 Should return a short, specific reason either way —
                 "passed because X" / "failed because Y" — not just
                 True/False, so a failing run is debuggable from the
                 report alone.
    setup_fn:    optional, runs before the task executes (e.g. create a
                 file to be found/moved, clean up leftovers from a
                 previous run)
    teardown_fn: optional, runs after assertion regardless of pass/fail
                 (e.g. delete a test file/folder created during the run)
    skip_reason: if set, the case is listed but not executed — used for
                 cases that need manual verification (things requiring
                 visual confirmation, audio, etc.) rather than removing
                 them from the suite entirely
    """
    name: str
    category: str
    task: str
    assert_fn: Callable[[dict], tuple[bool, str]]
    setup_fn: Optional[Callable[[], None]] = None
    teardown_fn: Optional[Callable[[], None]] = None
    skip_reason: Optional[str] = None
 
 
# ---------------------------------------------------------------------------
# Helpers shared across assertion functions
# ---------------------------------------------------------------------------
 
def _get_slots(final_state: dict):
    return final_state.get("slots")
 
 
def _last_step_results(final_state: dict) -> list:
    return final_state.get("step_results", [])
 
 
def _has_any_failed_step(final_state: dict) -> bool:
    """
    True if any step's FINAL recorded status was FAILED (i.e. it was
    never recovered by retry/replan and got skipped, or the whole task
    failed). Mirrors the same last-status-per-step logic added to
    complete_node in Phase 2.5b's follow-up fix.
    """
    from src.models import VerificationStatus
    last_status: dict[int, "VerificationStatus"] = {}
    for r in _last_step_results(final_state):
        last_status[r.step_number] = r.status
    return any(s == VerificationStatus.FAILED for s in last_status.values())
 
 
def _no_crash_and_no_silent_skip(final_state: dict) -> tuple[bool, str]:
    """
    Baseline check reused by several cases: the task must have reached
    a terminal state (is_done True) without is_failed, and without any
    step ending in a final FAILED status (a "soft skip" still counts as
    a problem worth flagging for a test case that's supposed to fully
    succeed, even though the agent itself treats skips as non-fatal).
    """
    if not final_state.get("is_done"):
        return False, "Task did not reach a terminal state (is_done=False)"
    if final_state.get("is_failed"):
        return False, f"Task failed: {final_state.get('ask_user_message') or final_state.get('last_error')}"
    if _has_any_failed_step(final_state):
        failed_nums = sorted({
            r.step_number for r in _last_step_results(final_state)
            if r.status.value == "failed"
        })
        return False, f"Step(s) {failed_nums} ended in FAILED status (skipped, not recovered)"
    return True, "Completed with no failed/skipped steps"
 
 
# ---------------------------------------------------------------------------
# Category A — Browser, site-native search (Phase 2.5c)
# ---------------------------------------------------------------------------
 
def _assert_youtube_play(final_state: dict) -> tuple[bool, str]:
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail
 
    slots = _get_slots(final_state)
    if not slots or not slots.browser_url:
        return False, "No browser_url in final slots"
 
    url = slots.browser_url
    if "youtube.com/watch" not in url:
        return False, f"Expected a youtube.com/watch URL, got: {url}"
 
    # Confirm the plan actually used the site-native search path, not
    # search_web (which would mean the Phase 2.5c fix regressed).
    plan = final_state.get("plan")
    actions_used = [s.action.value for s in plan.steps] if plan else []
    if "search_web" in actions_used:
        return False, (
            f"Plan used search_web (left YouTube for DuckDuckGo) instead "
            f"of search_on_page — regression of Phase 2.5c. Actions: {actions_used}"
        )
    if "search_on_page" not in actions_used:
        return False, f"Plan never used search_on_page. Actions: {actions_used}"
 
    return True, f"Landed on real video URL via site-native search: {url}"
 
 
def _assert_github_search(final_state: dict) -> tuple[bool, str]:
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail
 
    slots = _get_slots(final_state)
    if not slots or not slots.browser_url:
        return False, "No browser_url in final slots"
 
    if "github.com" not in slots.browser_url:
        return False, f"Expected to stay on github.com, got: {slots.browser_url}"
 
    return True, f"Stayed on GitHub, final URL: {slots.browser_url}"
 
 
# ---------------------------------------------------------------------------
# Category B — Browser, general research
# ---------------------------------------------------------------------------
 
def _research_save_setup():
    target = DESKTOP / "regression_test_python_news.txt"
    if target.exists():
        target.unlink()
 
 
def _research_save_teardown():
    target = DESKTOP / "regression_test_python_news.txt"
    if target.exists():
        target.unlink()
 
 
def _assert_research_and_save(final_state: dict) -> tuple[bool, str]:
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail
 
    target = DESKTOP / "regression_test_python_news.txt"
    if not target.exists():
        return False, f"Expected file not found: {target}"
 
    content = target.read_text(encoding="utf-8", errors="ignore")
    if len(content.strip()) < 100:
        return False, f"File exists but content suspiciously short ({len(content)} chars)"
 
    # Loose content check — don't pin to exact wording (that's brittle
    # against any future change in Gemini's summarization), but the
    # word "python" should appear given the task topic.
    if "python" not in content.lower():
        return False, "File content doesn't mention 'python' — likely wrong/empty research"
 
    return True, f"File written with {len(content)} chars of relevant content"
 
 
def _assert_research_summarize_only(final_state: dict) -> tuple[bool, str]:
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail
 
    slots = _get_slots(final_state)
    if not slots or not slots.last_text:
        return False, "No text content produced"
 
    if len(slots.last_text.strip()) < 50:
        return False, f"Summary suspiciously short ({len(slots.last_text)} chars)"
 
    return True, f"Produced {len(slots.last_text)} chars of summary text"
 
 
# ---------------------------------------------------------------------------
# Category C — File operations
# ---------------------------------------------------------------------------
 
def _create_folder_teardown():
    target = DESKTOP / "regression_test_folder"
    if target.exists():
        import shutil
        shutil.rmtree(target, ignore_errors=True)
 
 
def _assert_create_folder(final_state: dict) -> tuple[bool, str]:
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail
 
    target = DESKTOP / "regression_test_folder"
    if not target.is_dir():
        return False, f"Folder not found: {target}"
 
    return True, f"Folder created: {target}"
 
 
def _move_copy_setup():
    src_dir = DESKTOP / "regression_test_src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "regression_test_movefile.txt").write_text("test content for move/copy")
    dst_dir = DESKTOP / "regression_test_dst"
    dst_dir.mkdir(exist_ok=True)
    # Clear any stale copy from a previous failed run
    stale = dst_dir / "regression_test_movefile.txt"
    if stale.exists():
        stale.unlink()
 
 
def _move_copy_teardown():
    import shutil
    shutil.rmtree(DESKTOP / "regression_test_src", ignore_errors=True)
    shutil.rmtree(DESKTOP / "regression_test_dst", ignore_errors=True)
 
 
def _assert_copy_file(final_state: dict) -> tuple[bool, str]:
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail
 
    dst_file = DESKTOP / "regression_test_dst" / "regression_test_movefile.txt"
    if not dst_file.exists():
        return False, f"Copied file not found at destination: {dst_file}"
 
    src_file = DESKTOP / "regression_test_src" / "regression_test_movefile.txt"
    if not src_file.exists():
        return False, "Source file missing after COPY (should still exist — copy, not move)"
 
    return True, f"File correctly copied to {dst_file}, source preserved"
 
 
def _find_files_setup():
    target_dir = DESKTOP / "regression_test_findme"
    target_dir.mkdir(exist_ok=True)
    (target_dir / "alpha.pdf").write_bytes(b"fake pdf")
    (target_dir / "beta.pdf").write_bytes(b"fake pdf")
    (target_dir / "gamma.txt").write_text("not a pdf")
 
 
def _find_files_teardown():
    import shutil
    shutil.rmtree(DESKTOP / "regression_test_findme", ignore_errors=True)
 
 
def _assert_find_files(final_state: dict) -> tuple[bool, str]:
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail
 
    slots = _get_slots(final_state)
    if not slots or not slots.file_list:
        return False, "No file_list populated in slots"
 
    pdf_matches = [f for f in slots.file_list if f.lower().endswith(".pdf")]
    if len(pdf_matches) != 2:
        return False, f"Expected exactly 2 .pdf matches, found {len(pdf_matches)}: {slots.file_list}"
 
    return True, f"Correctly found {len(pdf_matches)} .pdf files"
 
 
# ---------------------------------------------------------------------------
# Category E — Failure handling (Phase 2.5a/2.5b regressions)
# ---------------------------------------------------------------------------
 
def _assert_last_step_failure_no_crash(final_state: dict) -> tuple[bool, str]:
    """
    This case is EXPECTED to have its last step fail (intentionally
    asks for an impossible click target). The point isn't "no failed
    steps" — it's "no crash, and an honest skip report", which is the
    exact Phase 2.5a regression target.
    """
    if not final_state.get("is_done"):
        return False, "Task did not reach a terminal state — possible crash or hang"
    if final_state.get("is_failed"):
        # Acceptable outcome too — depends on whether majority_done logic
        # decided to skip vs fail. Either is fine as long as it's not a
        # raw crash (which would raise an exception before is_done is
        # ever set at all, and the harness's own try/except would catch
        # that separately — see runner.py).
        return True, "Task correctly reported failure (no crash) for an impossible final step"
 
    failed_steps = {
        r.step_number for r in _last_step_results(final_state)
        if r.status.value == "failed"
    }
    if failed_steps:
        return True, f"Task completed, correctly skipped impossible step(s) {sorted(failed_steps)} (Phase 2.5a fix holds)"
 
    return True, "Task completed without needing to skip anything (target may have been findable after all)"
 
 
# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------
 
CASES: list[TestCase] = [
 
    # --- Category A: site-native search ---
    TestCase(
        name="youtube_search_and_play",
        category="A: site-native search",
        task="go to youtube.com and search for ishowspeed fifa 2026 official music video and play it",
        assert_fn=_assert_youtube_play,
    ),
    TestCase(
        name="github_search_stays_on_site",
        category="A: site-native search",
        task="go to github.com and search for langgraph",
        assert_fn=_assert_github_search,
    ),
 
    # --- Category B: general research ---
    TestCase(
        name="research_and_save_to_file",
        category="B: general research",
        task="search for latest python version and save to file regression_test_python_news.txt on Desktop",
        assert_fn=_assert_research_and_save,
        setup_fn=_research_save_setup,
        teardown_fn=_research_save_teardown,
    ),
    TestCase(
        name="research_summarize_only",
        category="B: general research",
        task="search for what langgraph is and tell me about it",
        assert_fn=_assert_research_summarize_only,
    ),
 
    # --- Category C: file operations ---
    TestCase(
        name="create_folder",
        category="C: file operations",
        task="create a folder called regression_test_folder on Desktop",
        assert_fn=_assert_create_folder,
        teardown_fn=_create_folder_teardown,
    ),
    TestCase(
        name="copy_file",
        category="C: file operations",
        task=f"copy the file regression_test_movefile.txt from {DESKTOP / 'regression_test_src'} to {DESKTOP / 'regression_test_dst'}",
        assert_fn=_assert_copy_file,
        setup_fn=_move_copy_setup,
        teardown_fn=_move_copy_teardown,
    ),
    TestCase(
        name="find_files_by_pattern",
        category="C: file operations",
        task=f"find all pdf files in {DESKTOP / 'regression_test_findme'}",
        assert_fn=_assert_find_files,
        setup_fn=_find_files_setup,
        teardown_fn=_find_files_teardown,
    ),
 
    # --- Category E: failure handling regressions ---
    TestCase(
        name="last_step_impossible_no_crash",
        category="E: failure handling",
        task="go to example.com and click the button labeled 'definitely_does_not_exist_xyz123'",
        assert_fn=_assert_last_step_failure_no_crash,
    ),
 
    # --- Category F: not yet covered, manual for now ---
    TestCase(
        name="spotify_play",
        category="D: app control",
        task="play song in spotify",
        assert_fn=lambda fs: _no_crash_and_no_silent_skip(fs),
        skip_reason="Requires Spotify to be installed and logged in — run manually if available",
    ),
    TestCase(
        name="ocr_fallback",
        category="F: not yet covered",
        task="open notepad, type Hello OCR Test, then find and click the text 'OCR' using OCR",
        assert_fn=lambda fs: _no_crash_and_no_silent_skip(fs),
        skip_reason="Needs visual confirmation that OCR actually located text correctly, not just non-crash",
    ),
]
 
 
def get_cases(category: Optional[str] = None) -> list[TestCase]:
    """Return all cases, or just those matching a category substring."""
    if category is None:
        return CASES
    return [c for c in CASES if category.lower() in c.category.lower()]