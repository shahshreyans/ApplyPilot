"""Remove jobs from the DB whose location fails the current location filter.

Runs automatically at the end of the discover stage (disable with
``location: {cleanup_after_discover: false}`` in searches.yaml), and is
also available as scripts/check_job_locations.py.
"""

import logging
import sqlite3

from applypilot.discovery.location import (
    REMOTE_MARKERS,
    LocationFilter,
    location_from_workday_url,
    location_ok,
    looks_like_us,
    matches_pattern,
)

log = logging.getLogger(__name__)


def rejection_reason(location: str | None, filt: LocationFilter) -> str:
    """Explain why a location fails the filter (assumes it does fail)."""
    if not location or not location.strip():
        return "unknown"
    loc = location.lower()
    if filt.exclude_remote and any(m in loc for m in REMOTE_MARKERS):
        return "remote"
    if any(matches_pattern(location, r) for r in filt.reject):
        return "reject pattern"
    if filt.exclude_us and looks_like_us(location):
        return "US"
    return "no accept pattern match"


def effective_location(row: sqlite3.Row) -> str | None:
    """Stored location, or one recovered from a Workday URL when it is blank."""
    if row["location"]:
        return row["location"]
    return location_from_workday_url(row["url"]) or location_from_workday_url(row["application_url"])


def find_rejected_jobs(conn: sqlite3.Connection, filt: LocationFilter) -> list[sqlite3.Row]:
    """All jobs whose (effective) location fails the filter."""
    rows = conn.execute(
        "SELECT url, application_url, title, location, site, discovered_at, applied_at, apply_status FROM jobs"
    ).fetchall()
    return [r for r in rows if not location_ok(effective_location(r), filt)]


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
