"""Shared location filtering for all discovery sources.

Reads the ``location`` block of searches.yaml:

    location:
      accept_patterns: [...]   # substrings that make a job location acceptable
      reject_patterns: [...]   # substrings that always disqualify a job
      exclude_us: true         # drop US-based jobs, including "Remote - US" ones
      exclude_remote: true     # drop remote/anywhere/WFH jobs entirely
      exclude_unknown: false   # drop jobs with no location at all
      cleanup_after_discover: true  # purge failing jobs from the DB after discover

Accept/reject patterns match whole words, case-insensitively ("SK" matches
"Regina, SK" but not "Gdansk").

``exclude_us`` defaults to true whenever the top-level ``country`` is set to
something other than the USA, so a Canadian search never keeps US postings
just because they are labelled remote.
"""

import re
from dataclasses import dataclass, field

REMOTE_MARKERS = ("remote", "anywhere", "work from home", "wfh", "distributed")

_US_COUNTRY_NAMES = {"usa", "us", "u.s.", "u.s", "u.s.a.", "u.s.a", "united states", "united states of america"}

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "district of columbia", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}

_CANADA_SIGNALS = {
    "canada", "ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt",
    "alberta", "british columbia", "manitoba", "new brunswick", "newfoundland and labrador",
    "nova scotia", "northwest territories", "nunavut", "ontario", "prince edward island",
    "quebec", "québec", "saskatchewan", "yukon",
}

_SPLIT_RE = re.compile(r"[,;|/()\-–—]+")
_WORKDAY_PATH_RE = re.compile(r"/job/([^/?#]+)/[^/?#]+/?(?:[?#].*)?$")


@dataclass
class LocationFilter:
    accept: list[str] = field(default_factory=list)
    reject: list[str] = field(default_factory=list)
    exclude_us: bool = False
    exclude_remote: bool = False
    exclude_unknown: bool = False


def _is_usa(country: str | None) -> bool:
    return bool(country) and country.strip().lower() in _US_COUNTRY_NAMES | {"america"}


def load_location_filter(search_cfg: dict | None) -> LocationFilter:
    """Build a LocationFilter from the search config.

    Supports the ``location: {accept_patterns, reject_patterns, exclude_us}``
    block as well as the legacy flat ``location_accept`` /
    ``location_reject_non_remote`` keys.
    """
    search_cfg = search_cfg or {}
    block = search_cfg.get("location")
    block = block if isinstance(block, dict) else {}

    accept = list(block.get("accept_patterns") or []) + list(search_cfg.get("location_accept") or [])
    reject = list(block.get("reject_patterns") or []) + list(search_cfg.get("location_reject_non_remote") or [])

    country = search_cfg.get("country")
    exclude_us = block.get("exclude_us")
    if exclude_us is None:
        exclude_us = bool(country) and not _is_usa(country)

    return LocationFilter(
        accept=accept,
        reject=reject,
        exclude_us=bool(exclude_us),
        exclude_remote=bool(block.get("exclude_remote", False)),
        exclude_unknown=bool(block.get("exclude_unknown", False)),
    )


def _words(text: str) -> str:
    """Lowercase, punctuation/hyphens -> single spaces, padded for " word " lookups."""
    return " " + " ".join(re.sub(r"[^a-z0-9.é]+", " ", text.lower()).split()) + " "


def looks_like_us(location: str | None) -> bool:
    """Heuristically decide whether a job location is in the United States."""
    if not location:
        return False

    tokens = [t.strip() for t in _SPLIT_RE.split(location) if t.strip()]
    lowered = [t.lower() for t in tokens]
    norm = _words(location)

    canadian = any(t in _CANADA_SIGNALS for t in lowered) or any(
        f" {name} " in norm for name in _CANADA_SIGNALS if len(name) > 2
    )

    if any(t in _US_COUNTRY_NAMES or t in US_STATE_NAMES for t in lowered):
        return True
    if any(f" {name} " in norm for name in _US_COUNTRY_NAMES):
        # e.g. "Remote US", "US Remote", "Anywhere in USA", "United-States-of-America-Eagan"
        return True
    if not canadian and any(f" {name} " in norm for name in US_STATE_NAMES):
        # e.g. "Cambridge-Massachusetts", "New York New York"
        return True

    for tok in tokens:
        if tok in US_STATE_CODES:
            # "CA" is also Canada's country code ("Toronto, ON, CA")
            if tok == "CA" and canadian:
                continue
            return True
    return False


def location_from_workday_url(url: str | None) -> str | None:
    """Recover a location from a Workday job URL or externalPath.

    ".../job/United-States-of-America-Eagan-Minnesota/Title_R1" ->
    "United States of America Eagan Minnesota"; "Warsaw---Poland" ->
    "Warsaw, Poland". Returns None when the URL has no location segment.
    """
    if not url:
        return None
    m = _WORKDAY_PATH_RE.search(url)
    if not m:
        return None
    seg = m.group(1).replace("---", ", ").replace("-", " ")
    return " ".join(seg.split()) or None


def matches_pattern(location: str, pattern: str) -> bool:
    """Case-insensitive whole-word match of an accept/reject pattern."""
    pat = pattern.strip().lower()
    if not pat:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(pat)}(?![a-z0-9])", location.lower()) is not None


def location_ok(location: str | None, filt: LocationFilter) -> bool:
    """Return True if a job location passes the user's location filter.

    Order: exclude_remote -> reject patterns -> US exclusion -> accept
    patterns -> remote. Unknown locations are kept (so the scorer can
    decide) unless exclude_unknown is set.
    """
    if not location or not location.strip():
        return not filt.exclude_unknown

    loc = location.lower()
    is_remote = any(m in loc for m in REMOTE_MARKERS)

    if filt.exclude_remote and is_remote:
        return False

    if any(matches_pattern(location, r) for r in filt.reject):
        return False

    if filt.exclude_us and looks_like_us(location):
        return False

    if any(matches_pattern(location, a) for a in filt.accept):
        return True

    return is_remote
