import sys
import os
 
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
 
from src.config import MEMORY_SIMILARITY_BACKEND
from src.memory import init_db, backfill_all_embeddings
 
 
def main() -> int:
    print(f"\n{'='*60}")
    print("  Phase 5 Embedding Backfill")
    print(f"{'='*60}\n")
 
    if MEMORY_SIMILARITY_BACKEND != "embedding":
        print(
            f"  MEMORY_SIMILARITY_BACKEND is '{MEMORY_SIMILARITY_BACKEND}', "
            "not 'embedding' — nothing to backfill."
        )
        print("  Set MEMORY_SIMILARITY_BACKEND=embedding (or unset it, it's")
        print("  the default) and re-run this script if you want vector memory.")
        print(f"\n{'='*60}\n")
        return 0
 
    init_db()
    print("  Scanning task_patterns for rows missing an embedding...\n")
 
    backfilled, skipped = backfill_all_embeddings()
 
    print(f"  Embedded : {backfilled}")
    print(f"  Skipped  : {skipped}  (already had one, or the API call failed)")
    print()
    if backfilled == 0 and skipped == 0:
        print("  No saved task patterns yet — nothing to do.")
    print(f"{'='*60}\n")
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
 