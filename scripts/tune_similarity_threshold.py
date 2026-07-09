"""
Phase 5 — EMBEDDING_SIMILARITY_THRESHOLD calibration.

This has to be run on a machine with a real GEMINI_API_KEY and network
access — it makes actual embed_content calls. Two modes:

  python scripts/tune_similarity_threshold.py
      Embeds a built-in set of labeled pairs (paraphrases that SHOULD
      match a past task, and unrelated pairs that SHOULDN'T) and prints
      the cosine similarity for each, plus a suggested threshold that
      best separates the two groups.

  python scripts/tune_similarity_threshold.py --from-db
      Embeds every task_description already in your memory.db and, for
      each one, prints its nearest neighbours sorted by similarity — so
      you can eyeball real data instead of synthetic examples. Doesn't
      suggest a number (there's no ground truth in this mode, just your
      own judgement of which pairs "should" have matched).

Either way, the output ends with the current EMBEDDING_SIMILARITY_THRESHOLD
from config.py and how it would score each example, so you can see whether
raising or lowering it changes the outcome before you touch the .env file.
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_PATH)

from src.config import EMBEDDING_SIMILARITY_THRESHOLD, MEMORY_SIMILARITY_BACKEND
from src.memory import init_db, _embed_text, _cosine_similarity, _get_conn


# Edit/extend this list with pairs that reflect the actual tasks you run
# through the agent — the more it looks like your real usage, the more
# useful the suggested threshold will be. Each tuple is:
#   (task_a, task_b, should_match: bool)
LABELED_PAIRS: list[tuple[str, str, bool]] = [
    # --- should match: same intent, different wording ---
    ("open spotify and play my playlist", "start up spotify and queue my liked songs", True),
    ("search github for the repo issues", "look up open issues on the github repository", True),
    ("check my gmail for unread messages", "see if I have any new emails", True),
    ("find the cheapest flight to tokyo", "search for a budget flight to tokyo", True),
    ("summarize this pdf for me", "give me a tldr of this document", True),
    ("close all chrome tabs", "shut every open browser tab", True),

    # --- should NOT match: different intent ---
    ("open spotify and play my playlist", "check my email for invoices", False),
    ("search github for the repo issues", "find the cheapest flight to tokyo", False),
    ("close all chrome tabs", "summarize this pdf for me", False),
    ("check my gmail for unread messages", "start up spotify and queue my liked songs", False),
    ("find the cheapest flight to tokyo", "shut every open browser tab", False),
    ("summarize this pdf for me", "look up open issues on the github repository", False),
]


def suggest_threshold(scored: list[tuple[float, bool]]) -> float | None:
    """
    Find the threshold that best separates should-match from
    shouldn't-match pairs (max margin between the lowest positive
    score and the highest negative score). Returns None if the groups
    overlap (no clean separator) so you know to expand LABELED_PAIRS
    or accept some error either way.
    """
    positives = [s for s, label in scored if label]
    negatives = [s for s, label in scored if not label]
    if not positives or not negatives:
        return None

    min_positive = min(positives)
    max_negative = max(negatives)

    if min_positive <= max_negative:
        return None  # groups overlap — no single threshold separates them cleanly

    return (min_positive + max_negative) / 2


def run_labeled_mode() -> None:
    print(f"\n{'='*70}")
    print("  EMBEDDING_SIMILARITY_THRESHOLD calibration — labeled pairs")
    print(f"{'='*70}\n")

    if MEMORY_SIMILARITY_BACKEND != "embedding":
        print(f"  MEMORY_SIMILARITY_BACKEND is '{MEMORY_SIMILARITY_BACKEND}', not "
              "'embedding' — set it to 'embedding' to calibrate this.")
        return

    scored: list[tuple[float, bool]] = []
    print(f"  {'similarity':>10}   {'expected':>10}   pair")
    print(f"  {'-'*10}   {'-'*10}   {'-'*50}")

    for task_a, task_b, should_match in LABELED_PAIRS:
        emb_a = _embed_text(task_a)
        emb_b = _embed_text(task_b)
        if emb_a is None or emb_b is None:
            print("  Embedding call failed — check GEMINI_API_KEY and network access.")
            return

        sim = _cosine_similarity(emb_a, emb_b)
        scored.append((sim, should_match))
        would_match = sim >= EMBEDDING_SIMILARITY_THRESHOLD
        flag = "  " if would_match == should_match else "!!"
        print(f"  {sim:>10.3f}   {'MATCH' if should_match else 'no match':>10}   "
              f"{flag} '{task_a}' <-> '{task_b}'")

    print()
    suggestion = suggest_threshold(scored)
    print(f"  Current EMBEDDING_SIMILARITY_THRESHOLD = {EMBEDDING_SIMILARITY_THRESHOLD}")
    if suggestion is not None:
        print(f"  Suggested threshold (midpoint of the gap)  = {suggestion:.3f}")
        print("\n  To apply: set EMBEDDING_SIMILARITY_THRESHOLD in your .env, e.g.")
        print(f"    EMBEDDING_SIMILARITY_THRESHOLD={suggestion:.3f}")
    else:
        print("  No clean separating threshold found — your should-match and")
        print("  shouldn't-match scores overlap. Either:")
        print("    - add more/clearer LABELED_PAIRS to this script and re-run, or")
        print("    - accept some false positives/negatives at any single threshold.")
    print(f"\n{'='*70}\n")


def run_db_mode() -> None:
    print(f"\n{'='*70}")
    print("  EMBEDDING_SIMILARITY_THRESHOLD calibration — real memory.db data")
    print(f"{'='*70}\n")

    if MEMORY_SIMILARITY_BACKEND != "embedding":
        print(f"  MEMORY_SIMILARITY_BACKEND is '{MEMORY_SIMILARITY_BACKEND}', not "
              "'embedding' — set it to 'embedding' to calibrate this.")
        return

    init_db()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, task_description FROM task_patterns"
        ).fetchall()

    if len(rows) < 2:
        print(f"  Only {len(rows)} saved task pattern(s) — need at least 2 to compare.")
        print("  Run some tasks through the agent first, or use the default")
        print("  labeled-pairs mode: python scripts/tune_similarity_threshold.py")
        return

    print(f"  Embedding {len(rows)} saved task descriptions...\n")
    embeddings = []
    for row in rows:
        vec = _embed_text(row["task_description"])
        if vec is None:
            print("  Embedding call failed — check GEMINI_API_KEY and network access.")
            return
        embeddings.append(vec)

    for i, row in enumerate(rows):
        sims = [
            (_cosine_similarity(embeddings[i], embeddings[j]), rows[j]["task_description"])
            for j in range(len(rows)) if j != i
        ]
        sims.sort(reverse=True)
        print(f"  '{row['task_description']}'")
        for sim, other_desc in sims[:3]:
            marker = "MATCH" if sim >= EMBEDDING_SIMILARITY_THRESHOLD else "     "
            print(f"      {marker}  {sim:.3f}   '{other_desc}'")
        print()

    print(f"  Current EMBEDDING_SIMILARITY_THRESHOLD = {EMBEDDING_SIMILARITY_THRESHOLD}")
    print("  Eyeball the above: any pair marked MATCH that shouldn't be, or any")
    print("  pair just under the threshold that should be, tells you which way")
    print("  to nudge EMBEDDING_SIMILARITY_THRESHOLD in your .env.")
    print(f"\n{'='*70}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-db", action="store_true",
        help="Calibrate against your real memory.db instead of built-in labeled pairs.",
    )
    args = parser.parse_args()

    if args.from_db:
        run_db_mode()
    else:
        run_labeled_mode()
    return 0


if __name__ == "__main__":
    sys.exit(main())