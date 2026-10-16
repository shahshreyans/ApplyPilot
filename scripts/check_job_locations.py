"""Check jobs already in the database against the current location filter.

Lists every job whose location would now be rejected by searches.yaml
(US jobs, remote jobs when exclude_remote is on, reject patterns, ...).
The discover stage already removes these automatically; use this to
inspect the DB or clean it without running discovery.

Usage (with the venv active, from the project folder):
    python scripts/check_job_locations.py            # report only
    python scripts/check_job_locations.py --delete   # also remove them

Jobs you have already applied to are never deleted.
"""

import argparse
import sys
from collections import Counter

from applypilot import config
from applypilot.database import get_connection
from applypilot.discovery.cleanup import (
    effective_location,
    find_rejected_jobs,
    purge_rejected_jobs,
    rejection_reason,
)
from applypilot.discovery.location import load_location_filter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delete", action="store_true", help="Delete the rejected jobs (applied jobs are kept).")
    parser.add_argument("--limit", type=int, default=50, help="Max rows to print (default 50, 0 = all).")
    args = parser.parse_args()

    filt = load_location_filter(config.load_search_config())
    print(f"Database: {config.DB_PATH}")
    print(f"Filter: exclude_us={filt.exclude_us} exclude_remote={filt.exclude_remote} "
          f"accept={filt.accept} reject={filt.reject}\n")
    if not filt.exclude_us:
        print("WARNING: US jobs are NOT being excluded. Set `country: \"Canada\"` "
              "or `location: {exclude_us: true}` in searches.yaml.\n")

    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    bad = find_rejected_jobs(conn, filt)
    reasons = Counter(rejection_reason(effective_location(r), filt) for r in bad)

    print(f"{total} jobs in DB, {len(bad)} fail the current location filter")
    for reason, n in reasons.most_common():
        print(f"  {reason}: {n}")
    print()

    shown = bad if args.limit == 0 else bad[:args.limit]
    for r in shown:
        loc = effective_location(r)
        print(f"[{rejection_reason(loc, filt):>8}] {loc or '?':<35.35} | "
              f"{(r['title'] or '?'):<40.40} | {r['site'] or '?'} | {(r['discovered_at'] or '')[:10]}")
    if len(shown) < len(bad):
        print(f"... and {len(bad) - len(shown)} more (use --limit 0 to see all)")

    if args.delete and bad:
        deleted, kept = purge_rejected_jobs(conn, filt)
        print(f"\nDeleted {deleted} jobs ({kept} already-applied jobs kept).")
    elif bad:
        print("\nRun again with --delete to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
