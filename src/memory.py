import sqlite3
import json
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher
from contextlib import contextmanager
from typing import Optional

from src.config import (
    MEMORY_DB_PATH,
    SIMILAR_TASK_THRESHOLD,
    MAX_MEMORY_HINTS,
    FAILURE_THRESHOLD,
)
from src.models import (
    TaskPattern,
    FailureRecord,
    UserPreference,
    ToolType,
    ActionType,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _get_conn():
    """Yield a connection and auto-commit/rollback."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema setup — called once at startup
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_patterns (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                task_description TEXT    NOT NULL,
                tool_sequence    TEXT    NOT NULL,  -- JSON list of ToolType values
                action_sequence  TEXT    NOT NULL,  -- JSON list of ActionType values
                apps_involved    TEXT    NOT NULL,  -- JSON list of app name strings
                success_rate     REAL    NOT NULL DEFAULT 1.0,
                last_used        TEXT    NOT NULL,
                avg_duration_ms  INTEGER
            );

            CREATE TABLE IF NOT EXISTS failure_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name         TEXT NOT NULL,
                tool_attempted   TEXT NOT NULL,
                action_attempted TEXT NOT NULL,
                failure_reason   TEXT NOT NULL,
                timestamp        TEXT NOT NULL,
                resolved         INTEGER NOT NULL DEFAULT 0  -- 0=false, 1=true
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                pref_key     TEXT NOT NULL UNIQUE,
                pref_value   TEXT NOT NULL,
                discovered_at TEXT NOT NULL
            );
        """)
    log.info("Memory DB initialised at %s", MEMORY_DB_PATH)


# ---------------------------------------------------------------------------
# Similarity helper
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    """
    Simple text similarity using SequenceMatcher (0.0 – 1.0).
    Good enough for task description matching without heavy dependencies.
    """
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------------------------------------------------------------------------
# Task pattern — write
# ---------------------------------------------------------------------------

def save_task_pattern(pattern: TaskPattern) -> None:
    """
    Upsert a task pattern.
    If a very similar task already exists, update its success_rate and
    last_used rather than creating a duplicate row.
    """
    existing = _find_similar_pattern(pattern.task_description)

    with _get_conn() as conn:
        if existing:
            # Update success rate as a rolling average
            new_rate = (existing["success_rate"] + pattern.success_rate) / 2
            conn.execute(
                """
                UPDATE task_patterns
                SET success_rate   = ?,
                    last_used      = ?,
                    avg_duration_ms = ?
                WHERE id = ?
                """,
                (new_rate, pattern.last_used, pattern.avg_duration_ms, existing["id"]),
            )
            log.debug("Updated existing pattern id=%s", existing["id"])
        else:
            conn.execute(
                """
                INSERT INTO task_patterns
                    (task_description, tool_sequence, action_sequence,
                     apps_involved, success_rate, last_used, avg_duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern.task_description,
                    json.dumps([t.value for t in pattern.tool_sequence]),
                    json.dumps([a.value for a in pattern.action_sequence]),
                    json.dumps(pattern.apps_involved),
                    pattern.success_rate,
                    pattern.last_used,
                    pattern.avg_duration_ms,
                ),
            )
            log.debug("Saved new task pattern: %s", pattern.task_description)


# ---------------------------------------------------------------------------
# Task pattern — read
# ---------------------------------------------------------------------------

def _find_similar_pattern(task_description: str) -> Optional[sqlite3.Row]:
    """
    Internal: return the single most-similar row above threshold, or None.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_patterns ORDER BY last_used DESC"
        ).fetchall()

    best_row   = None
    best_score = 0.0

    for row in rows:
        score = _similarity(task_description, row["task_description"])
        if score > best_score:
            best_score = score
            best_row   = row

    if best_score >= SIMILAR_TASK_THRESHOLD:
        return best_row
    return None


def get_memory_hints(task_description: str) -> str:
    """
    Return a formatted string of relevant past patterns for Brain's prompt.
    Includes up to MAX_MEMORY_HINTS similar successful patterns and any
    known failure paths for apps mentioned in the task.
    """
    hints: list[str] = []

    # --- similar successful patterns ---
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_patterns WHERE success_rate >= 0.5 ORDER BY last_used DESC"
        ).fetchall()

    scored = [
        (row, _similarity(task_description, row["task_description"]))
        for row in rows
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [row for row, score in scored if score >= SIMILAR_TASK_THRESHOLD]
    top = top[:MAX_MEMORY_HINTS]

    for row in top:
        tools   = json.loads(row["tool_sequence"])
        actions = json.loads(row["action_sequence"])
        hints.append(
            f"- Similar past task: '{row['task_description']}'\n"
            f"  Tools used in order: {', '.join(tools)}\n"
            f"  Actions: {', '.join(actions)}\n"
            f"  Success rate: {row['success_rate']:.0%}"
        )

    # --- known failure paths ---
    failure_hints = get_known_failures_for_task(task_description)
    if failure_hints:
        hints.append("Known failure paths to avoid:")
        hints.extend(failure_hints)

    if not hints:
        return ""

    return "MEMORY CONTEXT (use as hints, not strict rules):\n" + "\n".join(hints)


# ---------------------------------------------------------------------------
# Failure log — write
# ---------------------------------------------------------------------------

def log_failure(record: FailureRecord) -> None:
    """Persist a failure record."""
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO failure_log
                (app_name, tool_attempted, action_attempted,
                 failure_reason, timestamp, resolved)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.app_name,
                record.tool_attempted.value,
                record.action_attempted.value,
                record.failure_reason,
                record.timestamp,
                int(record.resolved),
            ),
        )
    log.debug("Logged failure for app=%s tool=%s", record.app_name, record.tool_attempted)


def mark_failure_resolved(app_name: str, tool: ToolType, action: ActionType) -> None:
    """Call this when a previously-failing path starts working again."""
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE failure_log SET resolved = 1
            WHERE app_name = ? AND tool_attempted = ? AND action_attempted = ?
            """,
            (app_name, tool.value, action.value),
        )


# ---------------------------------------------------------------------------
# Failure log — read
# ---------------------------------------------------------------------------

def get_known_failures_for_task(task_description: str) -> list[str]:
    """
    Return human-readable failure hints for any app mentioned in the task.
    Only returns unresolved failures that occurred at least FAILURE_THRESHOLD times.
    """
    task_lower = task_description.lower()

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT app_name, tool_attempted, action_attempted,
                   failure_reason, COUNT(*) as occurrences
            FROM failure_log
            WHERE resolved = 0
            GROUP BY app_name, tool_attempted, action_attempted
            HAVING occurrences >= ?
            """,
            (FAILURE_THRESHOLD,),
        ).fetchall()

    hints = []
    for row in rows:
        # Only surface if the app seems relevant to this task
        if row["app_name"].lower() in task_lower:
            hints.append(
                f"  - {row['app_name']}: {row['tool_attempted']} "
                f"/ {row['action_attempted']} failed {row['occurrences']}x "
                f"(reason: {row['failure_reason']})"
            )
    return hints


# ---------------------------------------------------------------------------
# User preferences — write
# ---------------------------------------------------------------------------

def save_preference(key: str, value: str) -> None:
    """Insert or replace a user preference."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (pref_key, pref_value, discovered_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pref_key) DO UPDATE SET
                pref_value    = excluded.pref_value,
                discovered_at = excluded.discovered_at
            """,
            (key, value, now),
        )
    log.debug("Saved preference: %s = %s", key, value)


# ---------------------------------------------------------------------------
# User preferences — read
# ---------------------------------------------------------------------------

def get_preference(key: str) -> Optional[str]:
    """Return a single preference value or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT pref_value FROM user_preferences WHERE pref_key = ?",
            (key,),
        ).fetchone()
    return row["pref_value"] if row else None


def get_all_preferences() -> dict[str, str]:
    """Return all preferences as a plain dict."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT pref_key, pref_value FROM user_preferences"
        ).fetchall()
    return {row["pref_key"]: row["pref_value"] for row in rows}


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Current UTC time as ISO string. Use this everywhere for consistency."""
    return datetime.now(timezone.utc).isoformat()


def clear_all_memory() -> None:
    """
    Wipe all tables. Useful for testing or fresh starts.
    Will ask for confirmation before deleting.
    """
    confirm = input("This will delete ALL memory. Type YES to confirm: ")
    if confirm.strip() == "YES":
        with _get_conn() as conn:
            conn.executescript("""
                DELETE FROM task_patterns;
                DELETE FROM failure_log;
                DELETE FROM user_preferences;
            """)
        log.warning("All memory cleared.")
    else:
        log.info("Clear cancelled.")
