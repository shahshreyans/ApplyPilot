import json

import pytest

from applypilot import config
from applypilot.database import init_db
from applypilot.enrichment.summary import (
    extract_job_summary,
    format_summary,
    job_context,
    normalize_summary,
    parse_summary,
    summarize_job,
)
from applypilot.profile_keywords import (
    candidate_skills,
    ensure_profile_keywords,
    normalize_resume_keywords,
    skill_overlap,
)
from applypilot.scoring.scorer import _candidate_block, _prefilter

DESCRIPTION = "We are hiring a backend developer. " * 10 + "Must know Python, Django and PostgreSQL."

SUMMARY = {
    "required_skills": ["Python", "Django", "PostgreSQL", "python"],
    "nice_to_have_skills": ["AWS"],
    "years_experience": "3+",
    "seniority": "mid",
    "education": None,
    "work_mode": "onsite",
    "domain": "fintech",
    "responsibilities": ["Build REST APIs", "Own deployments"],
    "ats_keywords": ["agile", "CI/CD"],
    "company_summary": "A payments startup.",
}


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return self.reply


ON = {"enabled": True, "store_full_description": True}


# -- normalize / parse ---------------------------------------------------------

def test_normalize_dedupes_and_validates():
    s = normalize_summary({**SUMMARY, "seniority": "wizard", "work_mode": "space"})
    assert s["required_skills"] == ["Python", "Django", "PostgreSQL"]
    assert s["seniority"] == "unknown" and s["work_mode"] == "unknown"
    assert s["education"] is None


def test_normalize_rejects_empty_summary():
    assert normalize_summary({"required_skills": [], "responsibilities": ["x"]}) is None
    assert normalize_summary("not json") is None
    assert normalize_summary(None) is None


def test_normalize_accepts_comma_string():
    assert normalize_summary({"required_skills": "Go, Rust"})["required_skills"] == ["Go", "Rust"]


def test_extract_job_summary_parses_fenced_json():
    client = FakeClient("```json\n" + json.dumps(SUMMARY) + "\n```")
    s = extract_job_summary("Backend Dev", "Acme", DESCRIPTION, client)
    assert s["required_skills"] == ["Python", "Django", "PostgreSQL"]
    assert client.calls == 1


def test_extract_job_summary_skips_short_descriptions():
    client = FakeClient(json.dumps(SUMMARY))
    assert extract_job_summary("x", "y", "too short", client) is None
    assert client.calls == 0


# -- prompt context --------------------------------------------------------------

def test_job_context_uses_summary():
    job = {"title": "Dev", "site": "Acme", "location": "Regina, SK",
           "full_description": DESCRIPTION, "job_summary": json.dumps(normalize_summary(SUMMARY))}
    text = job_context(job, ON)
    assert "JOB REQUIREMENTS" in text and "Required skills: Python, Django, PostgreSQL" in text
    assert "We are hiring" not in text


def test_job_context_falls_back_to_description():
    job = {"title": "Dev", "site": "Acme", "full_description": DESCRIPTION, "job_summary": None}
    assert "DESCRIPTION:\nWe are hiring" in job_context(job, ON)


def test_job_context_disabled_uses_description():
    job = {"title": "Dev", "site": "Acme", "full_description": DESCRIPTION,
           "job_summary": json.dumps(normalize_summary(SUMMARY))}
    assert "DESCRIPTION:" in job_context(job, {"enabled": False, "store_full_description": True})


def test_format_summary_lists_responsibilities():
    assert "- Build REST APIs" in format_summary(normalize_summary(SUMMARY))


# -- storage -------------------------------------------------------------------

def _db(tmp_path):
    conn = init_db(tmp_path / "t.db")
    conn.execute("INSERT INTO jobs (url, title, site, full_description) VALUES ('u1', 'Dev', 'Acme', ?)",
                 (DESCRIPTION,))
    conn.commit()
    return conn


@pytest.mark.parametrize("keep", [True, False])
def test_summarize_job_stores_summary(tmp_path, keep):
    conn = _db(tmp_path)
    job = dict(conn.execute("SELECT * FROM jobs WHERE url='u1'").fetchone())
    summarize_job(conn, job, {"enabled": True, "store_full_description": keep}, FakeClient(json.dumps(SUMMARY)))
    row = conn.execute("SELECT job_summary, summary_at, full_description FROM jobs WHERE url='u1'").fetchone()
    assert parse_summary(row["job_summary"])["required_skills"][0] == "Python"
    assert row["summary_at"]
    assert (row["full_description"] is not None) == keep


def test_summarize_job_records_failure(tmp_path):
    conn = _db(tmp_path)
    job = dict(conn.execute("SELECT * FROM jobs WHERE url='u1'").fetchone())
    summarize_job(conn, job, ON, FakeClient("no json here"))
    row = conn.execute("SELECT job_summary, summary_error, full_description FROM jobs WHERE url='u1'").fetchone()
    assert row["job_summary"] is None and row["summary_error"] and row["full_description"]


# -- resume keywords -------------------------------------------------------------

RESUME_KW = {
    "skills": {"languages": ["Python", "JavaScript"], "frameworks": ["Django", "React.js"],
               "databases": ["Postgres"], "cloud_devops": ["AWS"], "tools": ["Git"], "other": []},
    "keywords": ["REST APIs"], "job_titles": ["Backend Developer", "Full Stack Developer"],
    "seniority": "Mid", "years_experience": "3",
}


def test_normalize_resume_keywords():
    kw = normalize_resume_keywords(RESUME_KW)
    assert kw["years_experience"] == 3.0 and kw["seniority"] == "mid"
    assert normalize_resume_keywords({"skills": {}}) is None


def test_skill_overlap_handles_aliases():
    matched, missing = skill_overlap(
        ["Python", "PostgreSQL", "ReactJS", "Node.js", "Amazon Web Services (AWS)", "Kotlin"],
        ["python", "Postgres", "React", "NodeJS", "AWS"],
    )
    assert missing == ["Kotlin"] and len(matched) == 5


def test_ensure_profile_keywords_caches_by_resume_hash(tmp_path, monkeypatch):
    profile_path, resume_path = tmp_path / "profile.json", tmp_path / "resume.txt"
    profile_path.write_text(json.dumps({"skills_boundary": {"languages": ["Python"]}}))
    resume_path.write_text("Python developer with Django experience")
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)

    client = FakeClient(json.dumps(RESUME_KW))
    kw = ensure_profile_keywords(client=client)
    assert kw["job_titles"] == ["Backend Developer", "Full Stack Developer"]
    saved = json.loads(profile_path.read_text())
    assert saved["resume_keywords"]["resume_hash"]
    # Python is already in the boundary, so only the new skills are added
    assert "Python" not in saved["skills_boundary"]["resume_skills"]
    assert "Django" in saved["skills_boundary"]["resume_skills"]
    assert saved["skills_boundary"]["languages"] == ["Python"]

    ensure_profile_keywords(client=client)
    assert client.calls == 1  # unchanged resume -> cached

    resume_path.write_text("Python developer with Django and Go experience")
    ensure_profile_keywords(client=client)
    assert client.calls == 2  # resume changed -> re-extracted

    assert "AWS" in candidate_skills(json.loads(profile_path.read_text()))


def test_ensure_profile_keywords_without_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "missing.json")
    assert ensure_profile_keywords(client=FakeClient("{}")) is None


# -- scoring helpers ---------------------------------------------------------------

JOB = {"title": "Dev", "site": "Acme", "job_summary": json.dumps(normalize_summary(SUMMARY))}


def test_candidate_block_reports_overlap():
    block = _candidate_block(JOB, ["Python", "Postgres"])
    assert "SKILL MATCH: 2/3" in block and "missing: Django" in block


def test_prefilter_off_by_default():
    assert _prefilter(JOB, ["Java"], 0) is None


def test_prefilter_scores_low_overlap_jobs():
    result = _prefilter(JOB, ["Java"], 2)
    assert result["score"] == 1 and "0/3" in result["reasoning"]
    assert _prefilter(JOB, ["Python", "Django"], 2) is None


def test_detail_prompt_formats():
    detail = pytest.importorskip("applypilot.enrichment.detail")
    from applypilot.enrichment.summary import SUMMARY_RULES, SUMMARY_SCHEMA

    prompt = detail.DETAIL_EXTRACT_PROMPT.format(
        url="u", title="t", content="<p>x</p>", summary_schema=SUMMARY_SCHEMA, summary_rules=SUMMARY_RULES)
    assert '"summary"' in prompt and "required_skills" in prompt
