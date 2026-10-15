"""Remove jobs from the DB whose location fails the current location filter.

Runs automatically at the end of the discover stage (disable with
``location: {cleanup_after_discover: false}`` in searches.yaml), and is
also available as scripts/check_job_locations.py.
"""

import logging
import sqlite3

from applypilot.discovery.location import REMOTE_MARKERS, LocationFilter, location_ok, looks_like_us

log = logging.getLogger(__name__)


def rejection_reason(location: str, filt: LocationFilter) -> str:
    """Explain why a location fails the filter (assumes it does fail)."""
    loc = location.lower()
    if filt.exclude_remote and any(m in loc for m in REMOTE_MARKERS):
        return "remote"
    if any(r.lower() in loc for r in filt.reject):
        return "reject pattern"
    if filt.exclude_us and looks_like_us(location):
        return "US"
    return "no accept pattern match"


def find_rejected_jobs(conn: sqlite3.Connection, filt: LocationFilter) -> list[sqlite3.Row]:
    """All jobs whose location fails the filter."""
    rows = conn.execute(
        "SELECT url, title, location, site, discovered_at, applied_at, apply_status FROM jobs"
    ).fetchall()
    return [r for r in rows if not location_ok(r["location"], filt)]


def _is_applied(row: sqlite3.Row) -> bool:
    return bool(row["applied_at"]) or row["apply_status"] == "applied"


def purge_rejected_jobs(conn: sqlite3.Connection, filt: LocationFilter) -> tuple[int, int]:
    """Delete jobs that fail the filter, keeping ones already applied to.

    Returns (deleted, kept_applied).
    """
    bad = find_rejected_jobs(conn, filt)
    to_delete = [r["url"] for r in bad if not _is_applied(r)]
    if to_delete:
        conn.executemany("DELETE FROM jobs WHERE url = ?", [(u,) for u in to_delete])
        conn.commit()
    return len(to_delete), len(bad) - len(to_delete)
