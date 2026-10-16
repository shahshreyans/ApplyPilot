"""Job summaries: compact, structured keyword extracts of job descriptions.

The enrich stage turns each job description into a small JSON summary
(required skills, years of experience, responsibilities, ATS keywords, ...).
Scoring, tailoring and cover letters then send that summary to the LLM
instead of up to 6,000 characters of raw description.

Configured in searches.yaml:

    job_summary:
      enabled: true                  # build and use summaries
      store_full_description: true   # false = drop the full text once summarized
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from applypilot import config

log = logging.getLogger(__name__)

# Caps keep the summary small no matter how chatty the LLM is.
_LIST_FIELDS = {
    "required_skills": 25,
    "nice_to_have_skills": 15,
    "responsibilities": 6,
    "ats_keywords": 25,
}
_TEXT_FIELDS = ("years_experience", "seniority", "education", "work_mode", "domain", "company_summary")
_SENIORITY = {"intern", "junior", "mid", "senior", "lead", "principal", "manager", "unknown"}
_WORK_MODES = {"onsite", "hybrid", "remote", "unknown"}

SUMMARY_SCHEMA = (
    '{"required_skills": ["..."], "nice_to_have_skills": ["..."], '
    '"years_experience": "3+" or null, '
    '"seniority": "intern|junior|mid|senior|lead|principal|manager|unknown", '
    '"education": "..." or null, "work_mode": "onsite|hybrid|remote|unknown", '
    '"domain": "industry/product area" or null, '
    '"responsibilities": ["short phrase", "..."], '
    '"ats_keywords": ["..."], "company_summary": "one sentence" or null}'
)

SUMMARY_RULES = """Rules:
- required_skills: concrete must-have skills, languages, frameworks, tools, platforms, certifications.
- nice_to_have_skills: skills listed as preferred / bonus / nice to have.
- responsibilities: up to 6 short phrases (max 12 words each) describing the actual work.
- ats_keywords: other terms an ATS would match (methodologies, domains, soft skills that are explicitly asked for).
- Use the exact spelling from the posting. No duplicates. Do not invent anything that is not in the posting.
- years_experience: e.g. "3+", "2-5"; null if not stated."""

SUMMARY_PROMPT = f"""You extract the key requirements from a job posting for resume matching.

Return ONLY valid JSON (no markdown, no commentary) in exactly this shape:
{SUMMARY_SCHEMA}

{SUMMARY_RULES}"""


# -- Settings -----------------------------------------------------------------

def load_summary_settings(search_cfg: dict | None = None) -> dict:
    """Read the `job_summary` block of searches.yaml (with defaults)."""
    if search_cfg is None:
        try:
            search_cfg = config.load_search_config() or {}
        except Exception:
            search_cfg = {}
    block = search_cfg.get("job_summary")
    block = block if isinstance(block, dict) else {}
    return {
        "enabled": bool(block.get("enabled", True)),
        "store_full_description": bool(block.get("store_full_description", True)),
    }


# -- Parsing ------------------------------------------------------------------

def _clean_list(value, cap: int) -> list[str]:
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, (str, int, float)):
            continue
        text = " ".join(str(item).split()).strip(" .;-")
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text[:120])
        if len(out) >= cap:
            break
    return out


def normalize_summary(data) -> dict | None:
    """Validate and trim an LLM summary. Returns None if it has no skills at all."""
    if isinstance(data, str):
        data = parse_summary(data)
    if not isinstance(data, dict):
        return None

    summary: dict = {name: _clean_list(data.get(name), cap) for name, cap in _LIST_FIELDS.items()}
    for name in _TEXT_FIELDS:
        value = data.get(name)
        summary[name] = " ".join(str(value).split())[:200] if value not in (None, "", "null") else None

    if summary["seniority"] and summary["seniority"].lower() not in _SENIORITY:
        summary["seniority"] = "unknown"
    if summary["work_mode"] and summary["work_mode"].lower() not in _WORK_MODES:
        summary["work_mode"] = "unknown"

    if not (summary["required_skills"] or summary["nice_to_have_skills"] or summary["ats_keywords"]):
        return None
    return summary


def parse_summary(text: str | None) -> dict | None:
    """Load a summary stored as JSON text in the DB."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# -- LLM extraction -----------------------------------------------------------

def extract_job_summary(title: str, company: str, description: str, client=None) -> dict | None:
    """Ask the LLM for a structured summary of one job description."""
    if not description or len(description.strip()) < 100:
        return None

    from applypilot.discovery.smartextract import extract_json

    if client is None:
        from applypilot.llm import get_client
        client = get_client()

    messages = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": (
            f"TITLE: {title or 'N/A'}\nCOMPANY: {company or 'N/A'}\n\n"
            f"JOB DESCRIPTION:\n{description[:12000]}"
        )},
    ]
    raw = client.chat(messages, max_tokens=1024, temperature=0.0)
    try:
        return normalize_summary(extract_json(raw))
    except ValueError:
        return None


def save_summary(
    conn: sqlite3.Connection,
    url: str,
    summary: dict | None,
    error: str | None = None,
    store_full_description: bool = True,
) -> None:
    """Persist a summary (or the reason it failed) for one job."""
    now = datetime.now(timezone.utc).isoformat()
    if summary:
        conn.execute(
            "UPDATE jobs SET job_summary = ?, summary_at = ?, summary_error = NULL WHERE url = ?",
            (json.dumps(summary, ensure_ascii=False), now, url),
        )
        if not store_full_description:
            conn.execute("UPDATE jobs SET full_description = NULL WHERE url = ?", (url,))
    else:
        conn.execute(
            "UPDATE jobs SET summary_error = ? WHERE url = ?",
            (error or "no skills found in description", url),
        )
    conn.commit()


def summarize_job(conn: sqlite3.Connection, job: dict, settings: dict | None = None, client=None) -> dict | None:
    """Build, store and return the summary for one job dict (needs full_description)."""
    settings = settings or load_summary_settings()
    try:
        summary = extract_job_summary(job.get("title"), job.get("site"), job.get("full_description") or "", client)
        error = None
    except Exception as e:
        summary, error = None, f"LLM error: {e}"
        log.warning("Summary failed for '%s': %s", (job.get("title") or "?")[:60], e)
    save_summary(conn, job["url"], summary, error, settings["store_full_description"])
    if summary:
        job["job_summary"] = json.dumps(summary, ensure_ascii=False)
        if not settings["store_full_description"]:
            job["full_description"] = None
    return summary


def run_job_summaries(limit: int = 0, client=None) -> dict:
    """Summarize every job that has a description but no summary yet.

    Jobs whose summary already failed once (summary_error set) are skipped;
    later stages fall back to the full description for them.
    """
    settings = load_summary_settings()
    if not settings["enabled"]:
        log.info("Job summaries disabled (job_summary.enabled: false)")
        return {"summarized": 0, "failed": 0, "skipped": True}

    from applypilot.database import get_connection

    conn = get_connection()
    query = (
        "SELECT url, title, site, full_description FROM jobs "
        "WHERE full_description IS NOT NULL AND job_summary IS NULL AND summary_error IS NULL "
        "ORDER BY discovered_at DESC"
    )
    if limit > 0:
        query += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    if not rows:
        log.info("Job summaries: nothing to summarize")
        return {"summarized": 0, "failed": 0, "skipped": False}

    log.info("Job summaries: extracting keywords for %d jobs...", len(rows))
    done = failed = 0
    for i, job in enumerate(rows, 1):
        if summarize_job(conn, job, settings, client):
            done += 1
        else:
            failed += 1
        log.info("[%d/%d] summary %s  %s", i, len(rows), "ok" if job.get("job_summary") else "failed",
                 (job.get("title") or "?")[:60])
    log.info("Job summaries: %d ok, %d failed", done, failed)
    return {"summarized": done, "failed": failed, "skipped": False}


# -- Prompt helpers -----------------------------------------------------------

def format_summary(summary: dict) -> str:
    """Render a summary as compact prompt text."""
    lines = []
    labels = [
        ("required_skills", "Required skills"),
        ("nice_to_have_skills", "Nice to have"),
        ("years_experience", "Experience"),
        ("seniority", "Seniority"),
        ("education", "Education"),
        ("work_mode", "Work mode"),
        ("domain", "Domain"),
        ("ats_keywords", "ATS keywords"),
        ("company_summary", "Company"),
    ]
    for key, label in labels:
        value = summary.get(key)
        if isinstance(value, list):
            value = ", ".join(value)
        if value:
            lines.append(f"{label}: {value}")
    resp = summary.get("responsibilities") or []
    if resp:
        lines.append("Responsibilities:\n" + "\n".join(f"- {r}" for r in resp))
    return "\n".join(lines)


def job_context(job: dict, settings: dict | None = None, max_chars: int = 6000) -> str:
    """Job text for LLM prompts: the summary when available, else the description."""
    settings = settings or load_summary_settings()
    header = (
        f"TITLE: {job.get('title')}\n"
        f"COMPANY: {job.get('site')}\n"
        f"LOCATION: {job.get('location') or 'N/A'}\n\n"
    )
    summary = parse_summary(job.get("job_summary")) if settings["enabled"] else None
    if summary:
        return header + "JOB REQUIREMENTS (extracted from the posting):\n" + format_summary(summary)
    return header + f"DESCRIPTION:\n{(job.get('full_description') or job.get('description') or '')[:max_chars]}"
