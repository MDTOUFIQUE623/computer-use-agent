import sqlite3
import json
import logging
import math
import threading
from datetime import datetime, timezone
from difflib import SequenceMatcher
from contextlib import contextmanager
from typing import Optional

from src.config import (
    MEMORY_DB_PATH,
    SIMILAR_TASK_THRESHOLD,
    MAX_MEMORY_HINTS,
    FAILURE_THRESHOLD,
    MEMORY_SIMILARITY_BACKEND,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSIONALITY,
    EMBEDDING_SIMILARITY_THRESHOLD,
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

    # Phase 6 hardening: parallel_execute_node runs multiple worker threads
    # that each hit memory.db independently (memory hints during planning,
    # save_task_pattern at the end) — concurrently, not one-at-a-time like
    # every prior phase assumed. SQLite's default journal mode takes an
    # exclusive lock for the whole duration of a write, so two workers
    # writing at once can raise "database is locked" outright. WAL mode
    # lets readers and a writer proceed at the same time in the common
    # case, and busy_timeout makes sqlite3 retry internally for up to 5s
    # on the remaining writer-vs-writer contention instead of raising
    # immediately. Both are no-ops for the normal single-threaded case.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

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

        # --- Phase 5 migration: add embedding column to pre-existing DBs ---
        # SQLite has no "ADD COLUMN IF NOT EXISTS", so check PRAGMA first.
        # NULL for any row saved before Phase 5 (or saved while the
        # embedding API was unreachable) — those rows are backfilled
        # opportunistically the next time they're compared against, see
        # _get_row_embedding(), or all at once via backfill_all_embeddings().
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_patterns)")}
        if "embedding" not in cols:
            conn.execute("ALTER TABLE task_patterns ADD COLUMN embedding TEXT")
            log.info("Migrated task_patterns: added embedding column (Phase 5)")

    log.info("Memory DB initialised at %s", MEMORY_DB_PATH)


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    """
    Simple text similarity using SequenceMatcher (0.0 - 1.0).
    This is the Phase 1-4 backend. Still used as:
      (a) the whole-call fallback if the embedding API is unreachable, and
      (b) the per-row fallback for a legacy row whose embedding backfill
          itself just failed (e.g. API down at that exact moment).
    """
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# --- Phase 5: embedding backend -------------------------------------------
#
# One genai client for embeddings, created lazily so importing this module
# never requires GEMINI_API_KEY to be set (e.g. running with
# MEMORY_SIMILARITY_BACKEND=sequence, or in tests).

_embed_client = None
_embed_client_lock = threading.Lock()

# Process-lifetime cache: the same task_description gets embedded once on
# save and then again on every future similarity check, so avoid re-paying
# the API call for text we've already embedded this run.
_embedding_cache: dict[str, list[float]] = {}


def _get_embed_client():
    global _embed_client
    if _embed_client is None:
        with _embed_client_lock:
            if _embed_client is None:
                from google import genai
                _embed_client = genai.Client()
    return _embed_client


def _embed_text(text: str) -> Optional[list[float]]:
    """
    Return an embedding vector for `text`, or None if the embedding backend
    is disabled or unavailable right now (missing API key, network error,
    rate limit, etc). Callers must treat None as "fall back to
    SequenceMatcher for this comparison" rather than raising — memory is a
    nice-to-have hint for Brain, never something that should block a task.
    """
    if MEMORY_SIMILARITY_BACKEND != "embedding":
        return None

    if text in _embedding_cache:
        return _embedding_cache[text]

    try:
        from google.genai import types
        client = _get_embed_client()
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(
                output_dimensionality=EMBEDDING_DIMENSIONALITY
            ),
        )
        vector = list(result.embeddings[0].values)
        _embedding_cache[text] = vector
        return vector
    except Exception as e:
        log.warning(
            "Embedding call failed (%s) - falling back to SequenceMatcher "
            "for this comparison", e
        )
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-python cosine similarity - no numpy needed for 768-dim vectors."""
    dot     = sum(x * y for x, y in zip(a, b))
    norm_a  = math.sqrt(sum(x * x for x in a))
    norm_b  = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _get_row_embedding(row: sqlite3.Row) -> Optional[list[float]]:
    """
    Return the stored embedding for a task_patterns row, backfilling it
    (and persisting the backfill) if the row predates Phase 5 or was saved
    while the embedding API was down. Self-healing - no separate one-off
    migration script needed for the common case, though
    scripts/backfill_embeddings.py exists for bulk-backfilling a large
    existing memory.db in one pass instead of one row at a time.
    """
    raw = row["embedding"] if "embedding" in row.keys() else None
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("Corrupt embedding for pattern id=%s, re-embedding", row["id"])

    vector = _embed_text(row["task_description"])
    if vector is not None:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE task_patterns SET embedding = ? WHERE id = ?",
                (json.dumps(vector), row["id"]),
            )
        log.debug("Backfilled embedding for pattern id=%s", row["id"])
    return vector


def _score_row(
    query_text: str,
    query_embedding: Optional[list[float]],
    row: sqlite3.Row,
) -> tuple[float, float]:
    """
    Score one row against the query. Returns (score, threshold) in
    whichever scale actually produced the score - cosine similarity if
    both sides have an embedding, SequenceMatcher ratio otherwise. The two
    scales aren't directly comparable in absolute terms, so callers should
    rank by margin (score - threshold), not raw score.
    """
    if query_embedding is not None:
        row_embedding = _get_row_embedding(row)
        if row_embedding is not None:
            return (
                _cosine_similarity(query_embedding, row_embedding),
                EMBEDDING_SIMILARITY_THRESHOLD,
            )

    return _similarity(query_text, row["task_description"]), SIMILAR_TASK_THRESHOLD


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

    # Embed once up front so both the UPDATE and INSERT paths below have it.
    # None if the backend is disabled/unreachable — stored as NULL and
    # backfilled later by _get_row_embedding() when it's next compared.
    embedding = _embed_text(pattern.task_description)
    embedding_json = json.dumps(embedding) if embedding is not None else None

    with _get_conn() as conn:
        if existing:
            # Update success rate as a rolling average
            new_rate = (existing["success_rate"] + pattern.success_rate) / 2
            conn.execute(
                """
                UPDATE task_patterns
                SET success_rate   = ?,
                    last_used      = ?,
                    avg_duration_ms = ?,
                    embedding      = COALESCE(?, embedding)
                WHERE id = ?
                """,
                (new_rate, pattern.last_used, pattern.avg_duration_ms, embedding_json, existing["id"]),
            )
            log.debug("Updated existing pattern id=%s", existing["id"])
        else:
            conn.execute(
                """
                INSERT INTO task_patterns
                    (task_description, tool_sequence, action_sequence,
                     apps_involved, success_rate, last_used, avg_duration_ms,
                     embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern.task_description,
                    json.dumps([t.value for t in pattern.tool_sequence]),
                    json.dumps([a.value for a in pattern.action_sequence]),
                    json.dumps(pattern.apps_involved),
                    pattern.success_rate,
                    pattern.last_used,
                    pattern.avg_duration_ms,
                    embedding_json,
                ),
            )
            log.debug("Saved new task pattern: %s", pattern.task_description)


# ---------------------------------------------------------------------------
# Task pattern — read
# ---------------------------------------------------------------------------

def _find_similar_pattern(task_description: str) -> Optional[sqlite3.Row]:
    """
    Internal: return the single most-similar row above its threshold, or None.
    """
    query_embedding = _embed_text(task_description)

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_patterns ORDER BY last_used DESC"
        ).fetchall()

    best_row    = None
    best_margin = 0.0  # score - threshold; must be >= 0 to count as a match

    for row in rows:
        score, threshold = _score_row(task_description, query_embedding, row)
        margin = score - threshold
        if margin >= 0 and (best_row is None or margin > best_margin):
            best_margin = margin
            best_row    = row

    return best_row


def get_memory_hints(task_description: str) -> str:
    """
    Return a formatted string of relevant past patterns for Brain's prompt.
    Includes up to MAX_MEMORY_HINTS similar successful patterns and any
    known failure paths for apps mentioned in the task.
    """
    hints: list[str] = []

    # --- similar successful patterns ---
    query_embedding = _embed_text(task_description)

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_patterns WHERE success_rate >= 0.5 ORDER BY last_used DESC"
        ).fetchall()

    # margin = score - threshold, comparable across rows even when some are
    # scored via cosine similarity and others (pre-backfill) via
    # SequenceMatcher — see _score_row's docstring for why margin, not raw
    # score, is the right ranking key here.
    scored = [
        (row,) + _score_row(task_description, query_embedding, row)
        for row in rows
    ]
    scored = [(row, score - threshold) for row, score, threshold in scored]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [row for row, margin in scored if margin >= 0]
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
# Phase 5 — bulk embedding backfill
# ---------------------------------------------------------------------------

def backfill_all_embeddings() -> tuple[int, int]:
    """
    Compute and persist embeddings for every task_patterns row that doesn't
    have one yet. _get_row_embedding() already does this lazily, one row at
    a time, whenever an existing row happens to get compared against — this
    is the same thing but eager, for running once via
    scripts/backfill_embeddings.py right after upgrading to Phase 5 so a
    large existing memory.db doesn't pay the backfill cost gradually across
    many future task runs.

    Returns (backfilled_count, skipped_count) where skipped_count is rows
    that already had an embedding or where the API call failed.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, task_description, embedding FROM task_patterns"
        ).fetchall()

    backfilled = 0
    skipped    = 0

    for row in rows:
        if row["embedding"]:
            skipped += 1
            continue

        vector = _embed_text(row["task_description"])
        if vector is None:
            skipped += 1
            continue

        with _get_conn() as conn:
            conn.execute(
                "UPDATE task_patterns SET embedding = ? WHERE id = ?",
                (json.dumps(vector), row["id"]),
            )
        backfilled += 1

    log.info("Backfill complete: %d embedded, %d skipped", backfilled, skipped)
    return backfilled, skipped


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