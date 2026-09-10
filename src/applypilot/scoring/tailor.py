"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

Every application does ONE fresh, strictly-grounded rephrase of resume.txt
for exactly three things: Professional Summary, Experience bullets (job
scope + achievements), and Skills (a relevant subset of the master list).

Everything else -- header, Projects, Education, Software Skills, Languages,
Certifications -- is extracted VERBATIM from resume.txt and never touches
the LLM at all, so it can never drift, be fabricated, or reformatted. Job
titles, company names, and dates inside Experience are also frozen verbatim
(enforced in code, not just prompted) -- only job_scope/achievements bullet
wording may adapt to the target job's terminology.

No caching layer: since bullets are now allowed to vary per job (unlike the
old frozen-baseline design), there's nothing worth pre-computing. Each call
re-reads resume.txt fresh.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import LLMCircuitOpenError, get_client
from applypilot.scoring.region_quota import select_with_quota
from applypilot.scoring.validator import (
    BANNED_WORDS,
    company_variants,
    sanitize_text,
    validate_json_fields,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── resume.txt section extraction (frozen, verbatim, never LLM-touched) ───

_RESUME_SECTION_HEADERS = (
    "PROFESSIONAL EXPERIENCE", "EXPERIENCE", "PROJECTS", "EDUCATION",
    "CERTIFICATIONS", "LANGUAGES", "SKILLS", "SOFTWARE SKILLS", "TECHNICAL SKILLS",
)


def extract_resume_section(resume_text: str, header: str) -> str:
    """Extract a section's raw text verbatim from the master resume, from its
    header line up to (not including) the next known section header."""
    lines = resume_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().upper() == header.upper():
            start = i + 1
            break
    if start is None:
        return ""

    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().upper() in _RESUME_SECTION_HEADERS:
            end = i
            break

    section_lines = lines[start:end]
    while section_lines and not section_lines[0].strip():
        section_lines.pop(0)
    while section_lines and not section_lines[-1].strip():
        section_lines.pop()
    return "\n".join(section_lines)


def extract_summary(resume_text: str) -> str:
    """The About paragraph: the line(s) after the name/contact header, before
    the first section header."""
    lines = resume_text.splitlines()
    body_start = None
    for i, line in enumerate(lines):
        if line.strip().upper() in _RESUME_SECTION_HEADERS:
            body_start = i
            break
    if body_start is None:
        return ""
    summary_lines = [l.strip() for l in lines[2:body_start] if l.strip()]
    return " ".join(summary_lines).strip()


def extract_skills_list(resume_text: str) -> list[str]:
    """The master SKILLS line, split into individual items -- the ONLY pool
    a per-job tailoring pass may select from."""
    text = extract_resume_section(resume_text, "SKILLS")
    return [s.strip() for s in text.split("|") if s.strip()]


def parse_experience_entries(resume_text: str) -> list[dict]:
    """Parse PROFESSIONAL EXPERIENCE into structured entries -- header,
    company_line, job_scope bullets, achievements bullets -- all verbatim
    from resume.txt. This is the grounding truth the per-job rephrase works
    from, and the fallback the code falls back to if the LLM drops or
    reorders an entry.
    """
    text = extract_resume_section(resume_text, "PROFESSIONAL EXPERIENCE")
    if not text:
        text = extract_resume_section(resume_text, "EXPERIENCE")

    entries: list[dict] = []
    current: dict | None = None
    in_achievements = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        is_bullet = line.startswith("•") or line.startswith("-")
        if is_bullet:
            if current is None:
                continue
            bullet_text = line.lstrip("•-").strip()
            (current["achievements"] if in_achievements else current["job_scope"]).append(bullet_text)
        elif line.upper().rstrip(":") == "ACHIEVEMENTS":
            in_achievements = True
        elif current is None or current["company_line"]:
            # New entry's title line
            if current:
                entries.append(current)
            current = {"header": line, "company_line": "", "job_scope": [], "achievements": []}
            in_achievements = False
        else:
            # This entry's "Company - Location, Dates" line
            current["company_line"] = line

    if current:
        entries.append(current)
    return entries


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict) -> str:
    """Build the per-job tailoring prompt. Grounded strictly in the original
    resume text -- rephrasing and job-description terminology are allowed,
    invention is not."""
    resume_facts = profile.get("resume_facts", {})
    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"
    banned_str = ", ".join(BANNED_WORDS)

    preserved_titles = resume_facts.get("preserved_titles", [])
    titles_str = ", ".join(preserved_titles) if preserved_titles else "N/A"

    companies = resume_facts.get("preserved_companies", [])
    companies_str = ", ".join("/".join(company_variants(c)) for c in companies) if companies else "N/A"

    languages = profile.get("languages", {})
    languages_str = ", ".join(f"{lang} - {level}" for lang, level in languages.items())

    return f"""You are tailoring a resume to a specific job. You are given the candidate's ORIGINAL resume content (summary, experience, and master skills list) and a target job description.

## HARD RULE -- REPHRASE ONLY, NEVER INVENT:
Every bullet and the summary must describe ONLY work explicitly stated in the ORIGINAL RESUME you're given. You MAY:
- Reword for clarity and conciseness (tighten grammar, vary verbs, remove filler) or use synonym terminology from the relevant job description.
- Merge or split a bullet if it reads better, as long as no detail is added or lost.
You MUST NOT:
- Add any skill, tool, achievement, responsibility, or outcome not explicitly present in the original text.
- Infer what someone in this kind of role "would have" or "must have" done. If it is not written in the original, it does not go in the output.
- Invent or round numbers. Only these figures may appear: {metrics_str}.

## WHAT YOU MAY ADJUST FOR THIS JOB:
- Professional summary: this is NOT a generic paragraph reused across jobs. Read the job description's title and its "What You'll Be Doing" / "What You Bring" content, and open the summary by positioning the candidate for THIS type of role, then pull forward the 2-3 pieces of original experience that most directly match what this specific posting asks for. Every clause you write must point to ONE specific bullet in the original experience below -- if you can't name which original bullet a sentence came from, cut it. Do NOT turn a single one-off instance into a general claim of ongoing practice (example: one feature project's requirements-gathering is NOT "experience translating feedback into requirements and service improvements" as a general capability -- describe only what was actually done, on the actual thing it was done for). A summary that would read equally well pasted into any other job posting is a failure -- it must be obviously written for this one, but every word must still survive the zero-tolerance fact-check below.
- Experience: job scope and achievement bullets may be reworded to increase match with the job description, using its terminology, but every claim must remain true and traceable to the original -- never untrue.
- Skills: choose the skills most relevant to this role, but ONLY from the candidate's master skills list given below. Pick 6-10, ordered most-relevant-to-this-posting first. Prioritize skills that map directly to phrases in the job description's requirements/responsibilities over generic ones that merely could apply.

## WHAT NEVER CHANGES (copy exactly, verbatim, do not reword or translate):
- Each experience entry's job title and its "Company - Location, Dates" line, from this list: {titles_str}.
- Preserved companies: {companies_str} -- names stay as-is, every one must appear.
- LANGUAGE HONESTY: if language skills come up anywhere, state them EXACTLY as: {languages_str}. Never overstate.

## VOICE:
- Write like a real professional. Short, direct.
- BANNED WORDS (using ANY of these = validation failure -- do not use them even once): {banned_str}
- No em dashes. Use commas, periods, or hyphens.
- Never say "seasoned" or "seasoned professional".

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble. Return ALL experience entries given to you, in the same order, one JSON object per entry.

{{"summary":"2-3 sentences.","experience":[{{"header":"exact title from original","company_line":"exact Company - Location, Dates from original","job_scope":["bullet 1"],"achievements":["bullet 1","bullet 2"]}}],"skills":["Skill A","Skill B"]}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Zero-tolerance fact-checker for the per-job rephrase. Header, Projects,
    Education, Software Skills, Languages, and Certifications never reach the
    LLM at all, so this only ever needs to judge summary + experience bullets
    + the skills subset."""
    resume_facts = profile.get("resume_facts", {})
    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume fact-checker. A tailoring engine reworded a resume's summary and experience bullets, and picked a subset of skills, for a specific job. Your only job is to catch anything added, invented, or inferred that isn't explicitly in the original.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## ALLOWED:
- Rewording bullets/summary for clarity, conciseness, or job-description terminology, as long as the underlying facts and scope are unchanged.
- Reordering which point comes first.
- Selecting a subset of skills from the candidate's master list.

## FAIL FOR ANY OF THESE -- one instance is enough:
1. Any skill, tool, achievement, responsibility, or outcome in the summary or a bullet that is NOT explicitly present in the original resume text below.
2. Any claim inferred from "what someone in this role would typically do" rather than what the original text actually says.
3. Inventing or changing a number. The only real metrics are: {metrics_str}.
4. Any phrase implying more seniority, scope, or ownership than the original bullet states.

## NOT A FAILURE:
- Rewording that keeps the exact same facts and scope, even using job-posting terminology.
- Reordering points, combining/splitting a bullet with no detail added or lost.

Do not be lenient. If a claim isn't traceable to specific text in the original resume, it FAILS."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble)."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Deterministic safety nets (never trust the LLM alone for facts) ──────

def _enforce_verbatim_facts(llm_experience: list, original_entries: list[dict]) -> list[dict]:
    """Force header/company_line to match the original verbatim, positionally
    -- these are facts, not phrasing, and must never drift regardless of what
    the LLM returned. Falls back to the original bullets for any entry the
    LLM dropped, reordered, or malformed.
    """
    result = []
    for i, orig in enumerate(original_entries):
        entry = llm_experience[i] if i < len(llm_experience) and isinstance(llm_experience[i], dict) else {}
        job_scope = entry.get("job_scope")
        achievements = entry.get("achievements")
        result.append({
            "header": orig["header"],
            "company_line": orig["company_line"],
            "job_scope": job_scope if isinstance(job_scope, list) and job_scope else orig["job_scope"],
            "achievements": achievements if isinstance(achievements, list) and achievements else orig["achievements"],
        })
    return result


def _enforce_skills_subset(llm_skills, master_skills: list[str]) -> list[str]:
    """Keep only skills that are actually in the master list -- deterministic
    guard against the LLM inventing or misspelling a skill. Falls back to the
    full master list if nothing valid came back."""
    if not isinstance(llm_skills, list):
        return master_skills
    master_lower = {s.lower(): s for s in master_skills}
    result = [master_lower[s.lower()] for s in llm_skills if isinstance(s, str) and s.lower() in master_lower]
    return result or master_skills


# ── Resume Assembly ────────────────────────────────────────────────────────

def _judge_scope_text(data: dict) -> str:
    """The subset of a tailored resume the LLM actually influenced: summary,
    experience bullets. Everything else is frozen/verbatim and was never
    derived from the LLM in the first place."""
    lines = ["SUMMARY", str(data.get("summary", "")), "", "EXPERIENCE"]
    for entry in data.get("experience", []):
        lines.append(str(entry.get("header", "")))
        lines.extend(f"- {b}" for b in entry.get("job_scope", []))
        lines.extend(f"- {b}" for b in entry.get("achievements", []))
        lines.append("")
    return "\n".join(lines)


def assemble_resume_text(data: dict, profile: dict, resume_text: str) -> str:
    """Convert tailored JSON + frozen resume.txt sections into formatted text.

    Header (name, contact) is code-injected from the profile. Experience
    titles/company-lines are code-enforced verbatim (see
    _enforce_verbatim_facts) -- only job_scope/achievements bullet wording
    and the summary come from the LLM. Projects, Education, Software Skills,
    Languages, and Certifications are extracted verbatim from resume.txt and
    never touch the LLM at all.

    Args:
        data: Tailored JSON (summary, experience, skills, role_title -- the
              target job's own title, set by tailor_resume, code-injected
              verbatim, never LLM-generated).
        profile: User profile dict from load_profile().
        resume_text: The master resume text, for frozen-section extraction.

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile. Role title is the target
    # job's own posted title, copied verbatim (zero fabrication risk) -- not
    # an LLM-invented tagline. Contact line is label:value pairs so pdf.py
    # can lay it out in two columns without re-parsing ambiguous strings.
    lines.append(personal.get("full_name", ""))
    lines.append(str(data.get("role_title", "")))
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(f"Email: {personal['email']}")
    if personal.get("phone"):
        contact_parts.append(f"Phone: {personal['phone']}")
    if personal.get("city") and personal.get("country"):
        contact_parts.append(f"Location: {personal['city']}, {personal['country']}")
    if personal.get("linkedin_url"):
        contact_parts.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        contact_parts.append(f"GitHub: {personal['github_url']}")
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Summary
    lines.append("PROFESSIONAL SUMMARY")
    lines.append(sanitize_text(str(data.get("summary", ""))))
    lines.append("")

    # Experience -- header/company_line code-enforced verbatim, bullets per-job
    lines.append("PROFESSIONAL EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(str(entry.get("header", ""))))
        lines.append(sanitize_text(str(entry.get("company_line", ""))))
        for b in entry.get("job_scope", []):
            lines.append(f"- {sanitize_text(str(b))}")
        if entry.get("achievements"):
            lines.append("Achievements:")
            for b in entry["achievements"]:
                lines.append(f"- {sanitize_text(str(b))}")
        lines.append("")

    # Projects -- frozen, verbatim, never LLM-touched
    projects_text = extract_resume_section(resume_text, "PROJECTS")
    if projects_text:
        lines.append("PROJECTS")
        lines.append(projects_text)
        lines.append("")

    # Education -- frozen, verbatim, never LLM-touched
    lines.append("EDUCATION")
    education_text = extract_resume_section(resume_text, "EDUCATION")
    if education_text:
        lines.append(education_text)
    lines.append("")

    # Skills -- per-job relevant subset of the master list
    skills = data.get("skills", [])
    if skills:
        lines.append("SKILLS")
        lines.append(" | ".join(skills))
        lines.append("")

    # Software Skills -- frozen, verbatim, never LLM-touched
    software_skills_text = extract_resume_section(resume_text, "SOFTWARE SKILLS")
    if software_skills_text:
        lines.append("SOFTWARE SKILLS")
        lines.append(software_skills_text)
        lines.append("")

    # Languages -- frozen, verbatim, never LLM-touched
    languages_text = extract_resume_section(resume_text, "LANGUAGES")
    if languages_text:
        lines.append("LANGUAGES")
        lines.append(languages_text)
        lines.append("")

    # Certifications -- frozen, verbatim, never LLM-touched
    certifications_text = extract_resume_section(resume_text, "CERTIFICATIONS")
    if certifications_text:
        lines.append("CERTIFICATIONS")
        lines.append(certifications_text)

    return "\n".join(lines)


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss."""
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    response = client.chat(messages, max_tokens=512, temperature=0.1)

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume for one job: a fresh, grounded rephrase of
    resume.txt's Summary, Experience bullets, and a relevant Skills subset.
    Every other section is frozen/verbatim. Each retry starts a FRESH
    conversation (no apologetic spiral).

    Args:
        resume_text:      Master resume text (read fresh, not cached).
        job:               Job dict with title, company, location, full_description.
        profile:           User profile dict.
        max_retries:       Maximum retry attempts.
        validation_mode:   "strict", "normal", or "lenient".

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    original_summary = extract_summary(resume_text)
    original_entries = parse_experience_entries(resume_text)
    master_skills = extract_skills_list(resume_text)

    original_context = (
        f"ORIGINAL SUMMARY:\n{original_summary}\n\n"
        f"ORIGINAL EXPERIENCE:\n{json.dumps(original_entries, indent=2)}\n\n"
        f"MASTER SKILLS LIST (choose a subset ONLY from these, never add anything else):\n"
        f"{' | '.join(master_skills)}"
    )

    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('company') or 'Unknown'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    prompt_base = _build_tailor_prompt(profile)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        prompt = prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"{original_context}\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        raw = client.chat(messages, max_tokens=2048, temperature=0.4)

        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        if not data.get("summary") or not data.get("experience"):
            avoid_notes.append("Missing required field: summary or experience")
            continue

        # Deterministic guards -- facts are code-enforced, never trusted from the LLM alone
        data["experience"] = _enforce_verbatim_facts(data.get("experience"), original_entries)
        data["skills"] = _enforce_skills_subset(data.get("skills"), master_skills)
        data["role_title"] = job.get("title", "")

        validation = validate_json_fields(data, profile, mode=validation_mode)
        report["validator"] = validation

        if not validation["passed"]:
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            tailored = assemble_resume_text(data, profile, resume_text)
            report["status"] = "failed_validation"
            return tailored, report

        tailored = assemble_resume_text(data, profile, resume_text)

        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report

        judge = judge_tailored_resume(resume_text, _judge_scope_text(data), job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                continue
            report["status"] = "approved_with_judge_warning"
            return tailored, report

        report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 20,
                  validation_mode: str = "normal",
                  job_urls: list[str] | None = None) -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".
        job_urls:        If given, process exactly these URLs (any current
                         fit_score, ignores the geo quota) instead of
                         selecting new candidates.

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if job_urls:
        placeholders = ",".join("?" * len(job_urls))
        rows = conn.execute(f"SELECT * FROM jobs WHERE url IN ({placeholders})", job_urls).fetchall()
        columns = rows[0].keys() if rows else []
        jobs = [dict(zip(columns, row)) for row in rows]
    else:
        all_candidates = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=0)
        jobs = select_with_quota(all_candidates, limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    # Live freshness re-check: catches newly-closed listings and cross-checks
    # the apply URL against the real destination site before spending an LLM
    # call. See enrichment.detail.recheck_jobs.
    from applypilot.enrichment.detail import recheck_jobs
    recheck_stats = recheck_jobs([j["url"] for j in jobs])
    if recheck_stats.get("expired"):
        log.info("Live recheck: %d/%d shortlisted jobs are already closed -- skipping them.",
                 recheck_stats["expired"], len(jobs))
        if job_urls:
            placeholders = ",".join("?" * len(job_urls))
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE url IN ({placeholders}) "
                "AND (detail_error IS NULL OR detail_error != 'expired')", job_urls,
            ).fetchall()
            columns = rows[0].keys() if rows else []
            jobs = [dict(zip(columns, row)) for row in rows]
        else:
            all_candidates = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=0)
            jobs = select_with_quota(all_candidates, limit)
        if not jobs:
            log.info("No untailored jobs left after live recheck.")
            return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {"approved": 0, "failed_validation": 0, "failed_judge": 0, "error": 0}

    for job in jobs:
        completed += 1
        try:
            tailored, report = tailor_resume(resume_text, job, profile,
                                             validation_mode=validation_mode)

            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            txt_path = TAILORED_DIR / f"{prefix}.txt"
            txt_path.write_text(tailored, encoding="utf-8")

            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job.get('company') or 'Unknown'}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            pdf_path = None
            if report["status"] in ("approved", "approved_with_judge_warning"):
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                except Exception:
                    log.debug("PDF generation failed for %s", txt_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": str(txt_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except LLMCircuitOpenError as e:
            completed -= 1  # this job never actually got processed
            log.error("Aborting tailoring after %d/%d jobs -- %s", completed, len(jobs), e)
            break
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    now = datetime.now(timezone.utc).isoformat()
    _success_statuses = {"approved", "approved_with_judge_warning"}
    for r in results:
        if r["status"] in _success_statuses:
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, %d errors",
        elapsed,
        stats.get("approved", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("approved", 0),
        "failed": stats.get("failed_validation", 0) + stats.get("failed_judge", 0),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
