"""Check jobs already in the database against the current location filter.

Lists every job whose location would now be rejected by searches.yaml
(US jobs, remote jobs when exclude_remote is on, reject patterns, ...).
Jobs found before the location fix stay in the DB until they are removed.

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
from applypilot.discovery.location import REMOTE_MARKERS, load_location_filter, location_ok, looks_like_us


def _reason(location: str, filt) -> str:
    loc = location.lower()
    if filt.exclude_remote and any(m in loc for m in REMOTE_MARKERS):
        return "remote"
    if any(r.lower() in loc for r in filt.reject):
        return "reject pattern"
    if filt.exclude_us and looks_like_us(location):
        return "US"
    return "no accept pattern match"


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
    rows = conn.execute(
        "SELECT url, title, location, site, discovered_at, applied_at, apply_status FROM jobs"
    ).fetchall()

    bad = [r for r in rows if not location_ok(r["location"], filt)]
    reasons = Counter(_reason(r["location"], filt) for r in bad)

    print(f"{len(rows)} jobs in DB, {len(bad)} fail the current location filter")
    for reason, n in reasons.most_common():
        print(f"  {reason}: {n}")
    print()

    shown = bad if args.limit == 0 else bad[:args.limit]
    for r in shown:
        print(f"[{_reason(r['location'], filt):>8}] {r['location'] or '?':<35.35} | "
              f"{(r['title'] or '?'):<40.40} | {r['site'] or '?'} | {(r['discovered_at'] or '')[:10]}")
    if len(shown) < len(bad):
        print(f"... and {len(bad) - len(shown)} more (use --limit 0 to see all)")

    if args.delete and bad:
        to_delete = [r["url"] for r in bad if not r["applied_at"] and r["apply_status"] != "applied"]
        conn.executemany("DELETE FROM jobs WHERE url = ?", [(u,) for u in to_delete])
        conn.commit()
        print(f"\nDeleted {len(to_delete)} jobs ({len(bad) - len(to_delete)} already-applied jobs kept).")
    elif bad:
        print("\nRun again with --delete to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
