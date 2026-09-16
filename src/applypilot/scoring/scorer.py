"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import LLMCircuitOpenError, get_client

log = logging.getLogger(__name__)


# ── Seniority filter (deterministic) ──────────────────────────────────────
# Used here as a scoring dealbreaker, and imported by discovery/jobspy.py as
# a LinkedIn-only prefilter at discovery time (LinkedIn keyword search
# surfaces disproportionately many senior postings, so filtering early saves
# a wasted scrape+score on jobs that would be rejected here anyway).

_SENIOR_KEYWORDS = (
    "senior", "sr", "lead", "principal", "staff",
    "director", "head of", "vp", "vice president", "chief",
    "executive director", "group manager", "manager ii", "manager iii",
    "expert",
)

# Word-boundary matching, not substring -- titles routinely wrap the keyword
# in punctuation ("(Senior) Consultant", "Manager, Senior", "Sr.") that a
# plain "senior " substring check (with a hardcoded trailing space) misses
# entirely. \b matches at any word/non-word boundary, so it catches the
# keyword regardless of what punctuation surrounds it.
_SENIOR_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _SENIOR_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def is_senior_title(title: str | None) -> bool:
    if not title:
        return False
    return bool(_SENIOR_PATTERN.search(title))


# ── Graduate/university-entry scheme filter (deterministic) ─────────────
# The opposite problem from seniority: these programs are explicitly scoped
# to recent graduates / early-career hires (often with an eligibility window
# of 0-2 years post-degree), so a 6-year-experienced, dual-Masters candidate
# is overqualified for them, not a fit. They score well on the LLM path
# because the company is a priority employer and the industry matches --
# nothing about "graduate scheme" trips the seniority or coding dealbreakers,
# so without an explicit check they kept reaching the digest (UBS "Graduate
# Talent Program" / Julius Baer "University Graduate" postings, repeatedly).
# Phrase-based, not a bare "graduate" match -- that word alone appears in
# unrelated contexts ("graduate degree preferred") that must NOT be excluded.
_GRADUATE_PROGRAM_KEYWORDS = (
    "graduate talent program", "graduate talent programme",
    "graduate program", "graduate programme", "graduate scheme",
    "graduate trainee", "university graduate", "new graduate",
    "campus hire", "campus graduate",
)
_GRADUATE_PROGRAM_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _GRADUATE_PROGRAM_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def is_graduate_program_title(title: str | None) -> bool:
    if not title:
        return False
    return bool(_GRADUATE_PROGRAM_PATTERN.search(title))


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume, their job preferences, and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills, and the role is in a PRIORITY industry (see preferences) or fits their broader preferences perfectly.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged, industry is acceptable.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements or industry is borderline.
- 3-4: Weak match. Significant skill gaps or industry mismatch.
- 1-2: Poor match. Wrong field, wrong industry, or dealbreaker present.

AUTOMATIC SCORE OF 1-2 (dealbreakers — score this low regardless of other factors):
- Job description contains any excluded keyword (e.g. mandatory German C1)
- Company is in the excluded companies list
- Industry is in the excluded industries list (e.g. heavy machinery, defense, manufacturing, chemicals, construction)
- A REGION RULE is provided below and the company/role does not fit it (e.g. Dublin posting at a company that isn't
  big tech, or a Nice/Monaco posting at a non-tech company for a non-PM role)
- The job REQUIRES hands-on software engineering / coding ability as a core, load-bearing requirement (e.g. "strong
  coding skills in Python/Java/C++/JavaScript", "write production code", "X years of software development
  experience", "build and ship features") in a language that is NOT one of the candidate's actual programming
  languages (see CANDIDATE PREFERENCES below). This candidate is a business/product/project professional who does
  NOT write production code. Do NOT let AI/tech industry adjacency, "technical" job titles (Solutions Engineer,
  Forward Deployed Engineer, Technical Project Manager, etc.), or "transferable experience" override this --
  familiarity with a domain or using APIs/no-code tools is NOT the same as being required to hand-write code in a
  language the candidate doesn't know. This dealbreaker does NOT apply to roles where coding is a "nice to have" /
  peripheral skill rather than a core responsibility, or where the required language IS in the candidate's list.

IMPORTANT FACTORS:
- PRIORITY INDUSTRIES (see preferences below) get a scoring boost over other acceptable target industries, all else equal.
- PRIORITY COMPANIES (see preferences below) get a scoring boost when the job is at one of them, all else equal.
- Weight industry fit heavily overall — see the candidate's target industry list in the preferences below.
- Consider transferable experience (automation, API work, stakeholder management, digital transformation) -- but
  this does NOT extend to hands-on coding ability (see dealbreaker above).
- Factor in seniority level vs. job requirements
- Remote/hybrid preference matters

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
WHY: [2-4 short bullet points, each on its own line starting with "- ", naming a CONCRETE overlap between this posting and the candidate -- a named requirement, tool, industry or responsibility. No full sentences, no filler, max ~12 words each.]
MISSING: [ONE short line naming what this posting asks for that the candidate does not have, or what keeps it short of a 10. If a dealbreaker was found, name it here. CHECK LANGUAGE REQUIREMENTS FIRST: if the posting asks for a language at ANY level above the candidate's stated level (see the language levels in CANDIDATE PREFERENCES), that is the single most important gap and MUST be what this line names, quoting the level the posting asks for (e.g. "Asks for German at B2 minimum; candidate is B1"). Only if no such language gap exists should this line name something else. If genuinely nothing is missing, write "Nothing significant".]
REASONING: [2-3 sentences explaining the score, explicitly mentioning if a dealbreaker was found]"""

def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str, "why": str, "missing": str}

    `why` is newline-separated bullet text (no leading "- "); `missing` is one
    line. Both may be empty for a job scored before these fields existed, or
    when a deterministic dealbreaker short-circuits the LLM call entirely --
    callers must render them defensively.
    """
    score = 0
    keywords = ""
    reasoning = response
    why_lines: list[str] = []
    missing = ""

    section = None
    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            section = None
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            section = None
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("WHY:"):
            section = "why"
            rest = line.replace("WHY:", "").strip().lstrip("-").strip()
            if rest:
                why_lines.append(rest)
        elif line.startswith("MISSING:"):
            section = None
            missing = line.replace("MISSING:", "").strip()
        elif line.startswith("REASONING:"):
            section = None
            reasoning = line.replace("REASONING:", "").strip()
        elif section == "why" and line.startswith("-"):
            # WHY's bullets continue on their own lines until the next header.
            bullet = line.lstrip("-").strip()
            if bullet:
                why_lines.append(bullet)

    return {
        "score": score,
        "keywords": keywords,
        "reasoning": reasoning,
        "why": "\n".join(why_lines),
        "missing": missing,
    }


# ── Language check (deterministic) ──────────────────────────────────────
# Swiss/European postings are often bilingual boilerplate around an English
# core, so this only flags postings that are PREDOMINANTLY German or French --
# a few stray phrases ("Wir freuen uns auf Ihre Bewerbung", "Merci d'envoyer
# votre candidature") won't trip it. Only English-written postings are a
# match, in EVERY region including French-speaking Switzerland (Geneva,
# Lausanne) -- language honesty and readability both matter regardless of
# which canton the role is in.

_GERMAN_MARKERS = {
    "und", "der", "die", "das", "mit", "für", "wir", "sie", "ist", "sind",
    "ihre", "ihr", "unser", "unsere", "kenntnisse", "erfahrung", "aufgaben",
    "bewerbung", "gute", "sehr", "arbeiten", "team", "sowie", "auch",
    "einen", "eine", "als", "bei", "sich", "werden", "haben", "nicht",
}
_FRENCH_MARKERS = {
    "le", "la", "les", "des", "une", "un", "et", "vous", "votre", "nous",
    "notre", "est", "sont", "avec", "pour", "dans", "sur", "ce", "cette",
    "poste", "equipe", "équipe", "experience", "expérience", "mission",
    "missions", "profil", "candidat", "candidature", "competences",
    "compétences", "entreprise", "societe", "société",
}
_ENGLISH_MARKERS = {
    "the", "and", "with", "for", "you", "your", "our", "we", "is", "are",
    "experience", "team", "role", "responsibilities", "requirements",
    "application", "will", "have", "this", "that", "about", "join",
}


def _looks_non_english(job: dict) -> bool:
    """Heuristic: is the job posting's own text predominantly German or
    French (not English)?

    Independent of the excluded_keywords dealbreaker above, which only catches
    an explicit "German required" phrase -- this catches postings written
    entirely in German or French with no such phrase.
    """
    text = f"{job.get('title', '')} {job.get('full_description') or ''}".lower()
    words = re.findall(r"[a-zA-ZàâäéèêëïîôùûüçœÀÂÄÉÈÊËÏÎÔÙÛÜÇŒß]+", text)
    if len(words) < 30:
        return False
    sample = words[:400]
    en_hits = sum(1 for w in sample if w in _ENGLISH_MARKERS)
    de_hits = sum(1 for w in sample if w in _GERMAN_MARKERS)
    fr_hits = sum(1 for w in sample if w in _FRENCH_MARKERS)
    return (de_hits >= 8 and de_hits > en_hits) or (fr_hits >= 8 and fr_hits > en_hits)


# ── Region rules (company-tier requirements per city/country) ────────────

def _build_region_rule_text(job: dict, region_rules: dict) -> str:
    """Match the job's location against profile.job_preferences.region_rules.

    Returns the matching region's rule text, or "" if no region matches
    (e.g. a remote posting or an unlisted location -- no region dealbreaker
    applies in that case).
    """
    location = (job.get("location") or "").lower()
    if not location or not region_rules:
        return ""
    for region_name, cfg in region_rules.items():
        patterns = cfg.get("match", [])
        if any(p.lower() in location for p in patterns):
            return f"{region_name}: {cfg.get('rule', '')}"
    return ""


def _check_excluded_keywords(job: dict, excluded_keywords: list[str]) -> str | None:
    """Deterministic pre-check: does the job title/description contain a hard-excluded phrase?

    Returns the matched phrase, or None. Runs BEFORE the LLM call so a dealbreaker
    (e.g. "native German required") can never be missed by model judgment -- no API
    call is even made for jobs that hit this.
    """
    if not excluded_keywords:
        return None
    text = f"{job.get('title', '')} {job.get('full_description') or ''}".lower()
    for phrase in excluded_keywords:
        if phrase.lower() in text:
            return phrase
    return None


def _check_excluded_companies(job: dict, excluded_companies: list[str]) -> str | None:
    """Deterministic pre-check: is this job at a hard-excluded company?

    Checks the structured `company` field first (populated at discovery from
    JobSpy). Falls back to a text search over title + description for jobs
    discovered before that field existed, or from a source that didn't
    populate it -- the company name is almost always mentioned in the
    posting text even without structured data.
    """
    if not excluded_companies:
        return None
    company = (job.get("company") or "").lower()
    for name in excluded_companies:
        if name.lower() in company:
            return name
    text = f"{job.get('title', '')} {job.get('full_description') or ''}".lower()
    for name in excluded_companies:
        if name.lower() in text:
            return name
    return None


# At major consulting firms, "Manager" is a senior/experienced-hire grade
# (typically several years post-Consultant/Senior Consultant), unlike a tech
# "Product Manager" or "Project Manager" title which can be entry-level --
# so this is scoped to specific firms rather than making "manager" senior
# everywhere, which would wrongly exclude legitimate entry-level roles.
# The list is not limited to the global Big Four/MBB: mid-size and regional
# consultancies grade the same way and are easy to leave out.
#
# Note this rule is deliberately firm-scoped, NOT a blanket ban on "Manager".
# Operations Manager, Partnerships Manager, Program Manager and Business
# Manager are all target roles in profile.json -- excluding the word outright
# would gut the search.
_CONSULTING_FIRMS_MANAGER_IS_SENIOR = (
    "ey", "ernst & young", "ernst and young",
    "pwc", "pricewaterhousecoopers",
    "bcg", "boston consulting group",
    "deloitte", "accenture", "kpmg", "mckinsey", "bain",
    # Mid-size / Swiss-market consultancies, same grading ladder
    "synpulse", "capco", "oliver wyman", "roland berger", "kearney",
    "strategy&", "bearingpoint", "zeb", "horvath", "horváth",
    "simon-kucher", "alvarez & marsal", "ti&m", "zuhlke", "zühlke",
    "adnovum", "elca", "avaloq consulting", "infosys consulting",
    "cognizant consulting", "sopra steria", "msg", "cnlab",
)

_MANAGER_WORD_PATTERN = re.compile(r"\bmanager\b", re.IGNORECASE)
_CONSULTING_FIRM_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(f) for f in _CONSULTING_FIRMS_MANAGER_IS_SENIOR) + r")\b",
    re.IGNORECASE,
)


def is_consulting_manager_title(title: str | None, company: str | None) -> bool:
    """Is this a 'Manager'-titled role at one of the major consulting firms
    where that title specifically denotes a senior, experienced-hire grade?
    Shared with discovery/jobspy.py's LinkedIn-only prefilter."""
    if not company or not _CONSULTING_FIRM_PATTERN.search(company):
        return False
    return bool(_MANAGER_WORD_PATTERN.search(title or ""))


# Explicit "you must hand-write code" phrasing. The candidate's only real
# programming language is R (see skills_boundary.programming_languages) --
# none of these phrases ever legitimately describe an R requirement in
# practice, so presence alone is a reliable deterministic signal, unlike the
# nuanced "is this really a core requirement" judgment left to the LLM.
_HANDS_ON_CODING_PHRASES = (
    "strong coding ability", "strong coding skills", "excellent coding skills",
    "proficient in python", "proficient in java", "proficient in javascript",
    "expert in python", "expert in java", "expert in javascript",
    "write production code", "write clean code", "ship production code",
    "hands-on software development", "hands-on coding", "hands-on programming",
    "strong programming skills", "expert-level programming",
    "software engineering experience", "software development experience",
    "years of coding experience", "years of programming experience",
)


def _check_hands_on_coding(job: dict) -> str | None:
    """Deterministic pre-check: does this posting demand hands-on coding
    ability the candidate doesn't have? See dealbreaker list in SCORE_PROMPT
    for the nuanced version left to the LLM -- this catches the unambiguous
    phrasing cases directly, without spending an API call."""
    text = f"{job.get('title', '')} {job.get('full_description') or ''}".lower()
    for phrase in _HANDS_ON_CODING_PHRASES:
        if phrase in text:
            return phrase
    return None


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume and profile preferences."""
    profile = load_profile()
    prefs = profile.get("job_preferences", {})

    excluded_keywords = prefs.get("excluded_keywords", [])
    excluded_industries = prefs.get("excluded_industries", [])
    excluded_companies = prefs.get("excluded_companies", [])
    target_industries = prefs.get("target_industries", [])
    priority_industries = prefs.get("priority_industries", [])
    target_companies = prefs.get("target_companies", [])
    region_rules = prefs.get("region_rules", {})

    # Hard deterministic exclusion -- never rely on the LLM to catch a dealbreaker
    # phrase; check it directly, before spending an API call.
    matched = _check_excluded_keywords(job, excluded_keywords)
    if matched:
        return {"score": 1, "keywords": "", "reasoning": f"Excluded: contains required keyword '{matched}'"}

    matched_company = _check_excluded_companies(job, excluded_companies)
    if matched_company:
        return {"score": 1, "keywords": "", "reasoning": f"Excluded: company matches excluded company '{matched_company}'"}

    if _looks_non_english(job):
        return {"score": 1, "keywords": "", "reasoning": "Excluded: job posting appears to be written in German, not English"}

    matched_coding = _check_hands_on_coding(job)
    if matched_coding:
        return {"score": 2, "keywords": "", "reasoning": f"Excluded: requires hands-on coding ability ('{matched_coding}') the candidate doesn't have -- she is a business/product/project professional, not a software engineer"}

    # Seniority backstop: catches senior/lead/director postings regardless of
    # source site (the discovery-time filter in jobspy.py only applies to
    # LinkedIn results) and retroactively applies on any (re)score, including
    # backlog discovered before that filter existed.
    if is_senior_title(job.get("title")):
        return {"score": 1, "keywords": "", "reasoning": "Excluded: senior/lead/director-level title, candidate is targeting entry-level roles"}

    if is_graduate_program_title(job.get("title")):
        return {"score": 1, "keywords": "", "reasoning": "Excluded: university graduate / graduate talent program -- candidate has 6 years' experience and two Master's degrees, overqualified for an early-career entry scheme"}

    if is_consulting_manager_title(job.get("title"), job.get("company")):
        return {"score": 1, "keywords": "", "reasoning": f"Excluded: 'Manager' title at {job.get('company')} denotes a senior, experienced-hire grade at major consulting firms, candidate is targeting entry-level roles"}

    region_rule_text = _build_region_rule_text(job, region_rules)
    programming_languages = profile.get("skills_boundary", {}).get("programming_languages", [])
    languages = profile.get("languages", {})
    languages_text = ", ".join(f"{k} {v}" for k, v in languages.items()) or "not specified"

    preferences_text = f"""CANDIDATE PREFERENCES:
- Candidate's ACTUAL programming/coding ability (hard boundary): {", ".join(programming_languages) if programming_languages else "none -- not a coder"}. She is a business/product/project professional. A job requiring hands-on coding in a language outside this list, as a core requirement, is a near-total mismatch regardless of industry or "technical" job title.
- Target industries: {", ".join(target_industries) if target_industries else "any"}
- PRIORITY industries (score higher than other target industries, all else equal): {", ".join(priority_industries) if priority_industries else "none specified"}
- PRIORITY companies (score higher, all else equal -- these are companies the candidate specifically wants): {", ".join(target_companies) if target_companies else "none specified"}
- Excluded industries: {", ".join(excluded_industries) if excluded_industries else "none"}
- Excluded keywords (auto-score 1 if found): {", ".join(excluded_keywords) if excluded_keywords else "none"}
- Excluded companies (auto-score 1 if found): {", ".join(excluded_companies) if excluded_companies else "none"}
- Work preference: {prefs.get("remote_preference", "hybrid")}
- Company size preference: {profile.get("job_preferences", {}).get("company_size_preference", "any")}
- Language levels (exact -- a posting demanding a language ABOVE the level here is a major gap and the MISSING line must name it): {languages_text}
- REGION RULE for this job's location (auto-score 1 if the company/role does not fit): {region_rule_text or "none -- no region-specific restriction applies"}"""

    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('company') or 'Unknown (not provided by source -- infer from description if possible)'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\n{preferences_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except LLMCircuitOpenError:
        raise  # systemic failure -- let run_scoring abort the whole batch, don't swallow per-job
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}

def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        try:
            result = score_job(resume_text, job)
        except LLMCircuitOpenError as e:
            log.error(
                "Aborting scoring after %d/%d jobs -- %s",
                completed, len(jobs), e,
            )
            break
        result["url"] = job["url"]
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    # Write scores to DB
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, score_why = ?, "
            "score_missing = ?, scored_at = ? WHERE url = ?",
            (r["score"], f"{r['keywords']}\n{r['reasoning']}",
             r.get("why", ""), r.get("missing", ""), now, r["url"]),
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }


def rescore_urls(urls: list[str]) -> dict:
    """Re-score specific jobs, overwriting their existing score and fields.

    Used to refresh jobs that were scored before a rule or an output field
    changed -- e.g. entries scored before WHY/MISSING existed, which would
    otherwise reach a digest with prose instead of bullets and no stated gap.
    Unlike run_scoring(rescore=True) this touches only the URLs given.
    """
    if not urls:
        return {"scored": 0, "errors": 0}

    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    placeholders = ",".join("?" * len(urls))
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE url IN ({placeholders}) "
        "AND full_description IS NOT NULL",
        urls,
    ).fetchall()
    if not rows:
        return {"scored": 0, "errors": 0}
    jobs = [dict(zip(r.keys(), r)) for r in rows]

    now = datetime.now(timezone.utc).isoformat()
    scored = errors = 0
    for job in jobs:
        try:
            r = score_job(resume_text, job)
        except LLMCircuitOpenError as e:
            log.error("Rescore aborted after %d/%d -- %s", scored, len(jobs), e)
            break
        except Exception as e:
            log.error("Rescore failed for %s: %s", job.get("url"), e)
            errors += 1
            continue
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, score_why = ?, "
            "score_missing = ?, scored_at = ? WHERE url = ?",
            (r["score"], f"{r.get('keywords','')}\n{r.get('reasoning','')}",
             r.get("why", ""), r.get("missing", ""), now, job["url"]),
        )
        scored += 1
    conn.commit()
    log.info("Rescored %d job(s), %d error(s).", scored, errors)
    return {"scored": scored, "errors": errors}
