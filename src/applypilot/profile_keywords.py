"""Resume keywords: skills, ATS keywords and target titles pulled from resume.txt.

Stored in profile.json under ``resume_keywords`` and refreshed automatically
whenever resume.txt changes (tracked by a hash of its contents). Used to:
  - give the scorer the candidate's skill list and a skill-overlap count,
  - optionally add the resume's job titles as extra search queries,
  - fill ``skills_boundary.resume_skills`` so tailoring may use them.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from applypilot import config

log = logging.getLogger(__name__)

_SKILL_CATEGORIES = ("languages", "frameworks", "databases", "cloud_devops", "tools", "other")

RESUME_KEYWORDS_PROMPT = """You extract a candidate's skills from their resume for job matching.

Return ONLY valid JSON (no markdown, no commentary) in exactly this shape:
{"skills": {"languages": [], "frameworks": [], "databases": [], "cloud_devops": [], "tools": [], "other": []},
 "keywords": ["..."], "job_titles": ["..."], "seniority": "intern|junior|mid|senior|lead|principal",
 "years_experience": number or null}

Rules:
- skills: ONLY skills that actually appear in the resume. Exact spelling. No duplicates.
- keywords: up to 25 other ATS terms from the resume (domains, methodologies, certifications).
- job_titles: up to 5 job titles this person is a strong fit for, based on their experience.
- Do not invent anything that is not supported by the resume."""


def resume_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clean(values, cap: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        v = " ".join(v.split()).strip(" .;")
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
        if len(out) >= cap:
            break
    return out


def normalize_resume_keywords(data) -> dict | None:
    """Validate the LLM output. Returns None if no skills were found."""
    if not isinstance(data, dict):
        return None
    raw_skills = data.get("skills") if isinstance(data.get("skills"), dict) else {}
    skills = {cat: _clean(raw_skills.get(cat), 40) for cat in _SKILL_CATEGORIES}
    if not any(skills.values()):
        return None
    years = data.get("years_experience")
    try:
        years = float(years) if years not in (None, "") else None
    except (TypeError, ValueError):
        years = None
    return {
        "skills": skills,
        "keywords": _clean(data.get("keywords"), 25),
        "job_titles": _clean(data.get("job_titles"), 5),
        "seniority": str(data.get("seniority") or "").strip().lower() or None,
        "years_experience": years,
    }


def extract_resume_keywords(resume_text: str, client=None) -> dict | None:
    """Ask the LLM for the resume's skills, keywords and fitting job titles."""
    from applypilot.discovery.smartextract import extract_json

    if client is None:
        from applypilot.llm import get_client
        client = get_client()
    raw = client.chat(
        [{"role": "system", "content": RESUME_KEYWORDS_PROMPT},
         {"role": "user", "content": f"RESUME:\n{resume_text[:15000]}"}],
        max_tokens=1024, temperature=0.0,
    )
    try:
        return normalize_resume_keywords(extract_json(raw))
    except ValueError:
        return None


def _merge_into_boundary(profile: dict, kw: dict) -> int:
    """Add resume skills missing from skills_boundary under `resume_skills`. Returns count added."""
    boundary = profile.setdefault("skills_boundary", {})
    existing = {
        s.lower()
        for key, items in boundary.items()
        if key != "resume_skills" and isinstance(items, list)
        for s in items if isinstance(s, str)
    }
    extra = [s for cat in _SKILL_CATEGORIES for s in kw["skills"].get(cat, []) if s.lower() not in existing]
    if extra:
        boundary["resume_skills"] = _clean(extra, 80)
    else:
        boundary.pop("resume_skills", None)
    return len(extra)


def ensure_profile_keywords(force: bool = False, client=None) -> dict | None:
    """Return the resume keywords, re-extracting them if resume.txt changed.

    Never raises: on any problem (no profile, no resume, no LLM, LLM error)
    the previously stored keywords (or None) are returned.
    """
    try:
        profile = config.load_profile()
        resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ValueError):
        return None

    stored = profile.get("resume_keywords") if isinstance(profile.get("resume_keywords"), dict) else None
    current = resume_hash(resume_text)
    if stored and stored.get("resume_hash") == current and not force:
        return stored

    if client is None and config.get_tier() < 2:
        return stored

    try:
        kw = extract_resume_keywords(resume_text, client)
    except Exception as e:
        log.warning("Could not extract resume keywords: %s", e)
        return stored
    if not kw:
        log.warning("Could not extract resume keywords (no skills found)")
        return stored

    kw["resume_hash"] = current
    kw["updated_at"] = datetime.now(timezone.utc).isoformat()
    profile["resume_keywords"] = kw
    added = _merge_into_boundary(profile, kw)
    config.PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Resume keywords updated: %d skills, %d keywords, titles=%s (%d new skills added to skills_boundary)",
             sum(len(v) for v in kw["skills"].values()), len(kw["keywords"]), kw["job_titles"], added)
    return kw


# -- Matching -----------------------------------------------------------------

# Keys/values are already normalized (lowercase, no spaces, dots, dashes or underscores).
_ALIASES = {
    "js": "javascript", "ts": "typescript", "golang": "go", "postgres": "postgresql", "psql": "postgresql",
    "k8s": "kubernetes", "nodejs": "node", "reactjs": "react", "vuejs": "vue", "nextjs": "next",
    "amazonwebservices": "aws", "googlecloud": "gcp", "googlecloudplatform": "gcp",
    "microsoftazure": "azure", "dotnet": "net", "csharp": "c#", "mssql": "sqlserver",
    "restapi": "rest", "restapis": "rest", "restful": "rest", "cicd": "ci/cd",
}


def _norm(skill: str) -> str:
    s = re.sub(r"\s*\(.*?\)", "", skill.lower().strip())  # "Amazon Web Services (AWS)" -> "amazon web services"
    s = re.sub(r"[\s\-_.]+", "", s)
    return _ALIASES.get(s, s)


def candidate_skills(profile: dict) -> list[str]:
    """All of the candidate's skills: skills_boundary plus resume_keywords."""
    skills: list[str] = []
    for items in (profile.get("skills_boundary") or {}).values():
        if isinstance(items, list):
            skills.extend(s for s in items if isinstance(s, str))
    kw = profile.get("resume_keywords") or {}
    for items in (kw.get("skills") or {}).values():
        if isinstance(items, list):
            skills.extend(s for s in items if isinstance(s, str))
    return _clean(skills, 500)


def skill_overlap(job_skills: list[str], candidate: list[str]) -> tuple[list[str], list[str]]:
    """Split job skills into (matched, missing) against the candidate's skills."""
    have = {_norm(s) for s in candidate}
    matched, missing = [], []
    for skill in job_skills:
        (matched if _norm(skill) in have else missing).append(skill)
    return matched, missing
