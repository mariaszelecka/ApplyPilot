"""Cover letter generation: LLM-powered, profile-driven, with validation.

House style is a detailed, multi-paragraph letter (see
~/.applypilot/cover_letter_examples.txt) -- an opening paragraph naming the
role and the 3-4 things it asks for, then 3-5 themed paragraphs (one real
project or role per paragraph, each ending with an explicit bridge back to
the job's requirements), a language/logistics paragraph, and a one-line
close. The LLM only ever writes that body. The letterhead (name, contact,
date, company, subject line, salutation) and the sign-off block are
code-injected from the profile, so they can never drift or be fabricated --
mirroring how tailor.py freezes the resume's factual sections.

Every project, story, number, and skill in the body must be traceable to
either resume.txt or the two reference letters -- both are real, factual
accounts of the candidate's actual experience and are given to the LLM as
grounding, the same way tailor.py grounds resume bullets in resume.txt.
"""

import logging
import re
from datetime import datetime, timezone

from applypilot.config import COVER_LETTER_DIR, COVER_LETTER_EXAMPLES_PATH, RESUME_PATH, load_profile
from applypilot.database import get_connection
from applypilot.llm import LLMCircuitOpenError, get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_cover_letter_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


def _load_reference_letters() -> str:
    """The two real, previously-sent cover letters that define house style
    and voice, and are themselves a legitimate source of factual grounding
    (they describe real projects, some in more detail than resume.txt)."""
    if not COVER_LETTER_EXAMPLES_PATH.exists():
        return ""
    return COVER_LETTER_EXAMPLES_PATH.read_text(encoding="utf-8")


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict) -> str:
    """Build the cover letter BODY system prompt from the user's profile.

    Only the body -- opening paragraph through the closing line -- comes
    from the LLM. Salutation, subject line, and sign-off are code-injected
    in assemble_cover_letter_text and must never appear in the output.
    """
    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    languages = profile.get("languages", {})
    languages_str = ", ".join(f"{lang} - {level}" for lang, level in languages.items())

    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    return f"""Write the BODY of a cover letter for {sign_off_name}. This is a detailed, structured letter in an established house style -- NOT a short punchy pitch. Match the structure and depth of the reference letters you're given, which run roughly 450-700 words.

## STRUCTURE (follow exactly):

1. OPENING PARAGRAPH (2-3 sentences): "I am applying for the [job title] position. The role's combination of [3-4 specific things this posting asks for, named using its own language] maps directly onto work I have done across my [consulting and corporate / corporate] career." Do NOT add a filler closing sentence like "I want to make that case concretely" -- that exact phrase (and close variants of it) is banned. End the paragraph on the "maps directly onto..." sentence, optionally followed by one more concrete, specific sentence -- never a vague throat-clearing line.

2. THEMED BODY PARAGRAPHS (3-5 paragraphs): Before writing these, identify the specific responsibilities and requirements this posting actually lists (its "What You'll Be Doing" / "What You Bring" / requirements sections) and pick ONE real project or role from the candidate's resume or the reference letters for EACH one that best demonstrates it -- do not reach for a paragraph topic just because a good story exists for it if this job isn't actually asking for it. Each paragraph opens by naming its theme (e.g. "On requirements engineering and product ownership," or "On customer-facing technical work:") using language drawn from THIS posting, and then goes deep on that one specific project or role -- what was actually built, coordinated, or delivered, with real numbers where the original text has them. End the paragraph with an explicit bridge sentence tying that specific experience to what this posting asks for (e.g. "This experience directly reflects the [X] responsibilities described in this role." / "This is structurally close to what this role describes: [specific overlap].").

3. LANGUAGE & LOGISTICS PARAGRAPH (short, near the end): weave the language levels into one natural sentence (e.g. "I speak X natively, Y at an intermediate level with active development in progress, and Z at a basic-to-intermediate level."), NOT a raw comma-separated dump. Each language's level must match EXACTLY, word for word, what is stated here -- never a stronger word, never omit "in progress" or similar qualifiers: {languages_str}. Then mention the candidate's base location, work authorization, and availability, and note on-site/remote fit if the posting mentions it.

4. CLOSING LINE (1 sentence): a variation on "I would welcome the opportunity to discuss my application in more detail."

## HARD RULE -- REPHRASE AND REUSE ONLY, NEVER INVENT:
Every project, story, number, and skill you mention MUST come from the ORIGINAL RESUME or the REFERENCE COVER LETTERS you're given below -- both are real, factual accounts of this candidate's actual experience. You MAY reuse phrasing, framing, and close-to-verbatim sentences from the reference letters where they genuinely fit this job -- that is encouraged. You MUST NOT:
- Invent a new project, responsibility, tool, or outcome not traceable to one of those two sources.
- Infer what someone in this kind of role "would have" or "must have" done.
- Invent or round numbers. Only these figures may appear: {metrics_str} (plus any numbers already stated in the reference letters).

## VOICE:
- Structured, confident, detailed -- this letter goes deep on 3-5 concrete examples, matching the reference letters' register, not a terse 3-paragraph pitch.
- Em dashes are allowed and expected for asides, matching the reference letters' style.
- BANNED WORDS (using ANY of these = validation failure -- do not use them even once): {all_banned}
- ALSO BANNED (meta-commentary the validator catches): {leak_banned}

## OUTPUT: Return ONLY the body text -- start directly with "I am applying for..." and end with the closing line. Do NOT include "Dear ...", do NOT include a subject line, do NOT include "Kind regards" or a sign-off -- those are added separately. No markdown. No preamble."""


def _build_cover_letter_judge_prompt() -> str:
    """Zero-tolerance fact-checker for the cover letter body, scoped against
    BOTH resume.txt and the two reference letters -- either is a legitimate
    source of fact for this house style."""
    return """You are a cover-letter fact-checker. A writer produced a cover letter body grounded in a candidate's resume AND two of their own previously-sent, real cover letters (both given to you as source material). Your only job is to catch anything added, invented, or inferred that isn't traceable to one of those two sources.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## ALLOWED:
- Rewording or reframing a project/story from either source document, including close-to-verbatim reuse of phrasing from the reference letters.
- Choosing which projects/stories to feature and in what order.
- Restating language levels, permit status, and availability from the profile.

## FAIL FOR ANY OF THESE -- one instance is enough:
1. Any project, tool, responsibility, or outcome that is NOT explicitly present in the original resume or the reference letters.
2. Any claim inferred from "what someone in this role would typically do" rather than what the source text actually says.
3. Inventing or changing a number.
4. Any language-proficiency claim that overstates the candidate's real level (check it against what the resume/profile states, not what a reference letter happened to say -- the reference letters are not authoritative on this one point).
5. Any phrase implying more seniority, scope, or ownership than the source material states.

## NOT A FAILURE:
- Vivid or confident phrasing that stays within the facts.
- Reordering, combining, or condensing details from the source material.

Do not be lenient. If a claim isn't traceable to specific text in the resume or the reference letters, it FAILS."""


# ── Letterhead assembly (code-injected, never touches the LLM) ───────────

def assemble_cover_letter_text(body: str, job: dict, profile: dict) -> str:
    """Wrap the LLM-written body in a code-injected letterhead: name/contact,
    date, company/location, subject line, salutation, and sign-off. None of
    this reaches the LLM, so it can never drift or be fabricated -- the same
    principle as tailor.py's frozen resume sections.
    """
    personal = profile.get("personal", {})
    name = personal.get("full_name", "")
    city = personal.get("city", "")
    country = personal.get("country", "")

    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if city and country:
        contact_parts.append(f"{city}, {country}")
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    contact_line = " | ".join(contact_parts)
    company = job.get("company") or "the Hiring Team"

    lines = [
        name,
        contact_line,
        "",
        f"Dear {company} Team,",
        "",
        body.strip(),
        "",
        "Kind regards,",
        "",
        name,
    ]

    return "\n".join(lines)


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str) -> str:
    """Remove LLM preamble before 'I am applying' if present."""
    idx = text.lower().find("i am applying")
    if idx > 0:
        return text[idx:]
    return text


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> str:
    """Generate a cover letter for one job: a fresh, grounded body (resume.txt
    + the two reference letters), wrapped in a code-injected letterhead.

    Args:
        resume_text:      The candidate's resume text (base or tailored).
        job:              Job dict with title, company, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".

    Returns:
        The full assembled cover letter text (best attempt even if validation failed).
    """
    reference_letters = _load_reference_letters()
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('company') or 'Unknown'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )
    source_context = (
        f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\n"
        f"REFERENCE COVER LETTERS (real, previously sent -- house style AND a legitimate "
        f"source of fact):\n{reference_letters}"
    )

    avoid_notes: list[str] = []
    body = ""
    client = get_client()
    prompt_base = _build_cover_letter_prompt(profile)

    for attempt in range(max_retries + 1):
        prompt = prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"{source_context}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\n"
                "Write the cover letter body:"
            )},
        ]

        raw = client.chat(messages, max_tokens=1600, temperature=0.6)
        raw = sanitize_cover_letter_text(raw)
        body = _strip_preamble(raw)

        validation = validate_cover_letter(body, mode=validation_mode)
        if not validation["passed"]:
            avoid_notes.extend(validation["errors"])
            log.debug(
                "Cover letter attempt %d/%d failed: %s",
                attempt + 1, max_retries + 1, validation["errors"],
            )
            continue

        if validation_mode == "lenient":
            return assemble_cover_letter_text(body, job, profile)

        judge = _judge_cover_letter(body, resume_text, reference_letters, job.get("title", ""))
        if judge["passed"]:
            return assemble_cover_letter_text(body, job, profile)

        avoid_notes.append(f"Judge rejected: {judge['issues']}")
        log.debug("Cover letter attempt %d/%d rejected by judge: %s",
                  attempt + 1, max_retries + 1, judge["issues"])

    return assemble_cover_letter_text(body, job, profile)  # last attempt even if failed


def _judge_cover_letter(body: str, resume_text: str, reference_letters: str, job_title: str) -> dict:
    """LLM judge layer scoped to the body only, against both source documents."""
    judge_prompt = _build_cover_letter_judge_prompt()

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\n"
            f"REFERENCE COVER LETTERS:\n{reference_letters}\n\n---\n\n"
            f"COVER LETTER BODY TO JUDGE:\n{body}\n\n"
            "Judge this cover letter body:"
        )},
    ]

    client = get_client()
    response = client.chat(messages, max_tokens=512, temperature=0.1)

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {"passed": passed, "verdict": "PASS" if passed else "FAIL", "issues": issues, "raw": response}


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 7, limit: int = 20,
                      validation_mode: str = "normal",
                      job_urls: list[str] | None = None) -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum fit_score threshold.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".
        job_urls:        If given, process exactly these URLs instead of the
                         usual score-ordered selection.

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if job_urls:
        placeholders = ",".join("?" * len(job_urls))
        jobs = conn.execute(
            f"SELECT * FROM jobs WHERE url IN ({placeholders}) "
            "AND tailored_resume_path IS NOT NULL AND full_description IS NOT NULL",
            job_urls,
        ).fetchall()
    else:
        # Fetch jobs that have tailored resumes but no cover letter yet
        jobs = conn.execute(
            "SELECT * FROM jobs "
            "WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
            "AND full_description IS NOT NULL "
            "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
            "AND COALESCE(cover_attempts, 0) < ? "
            "ORDER BY fit_score DESC LIMIT ?",
            (min_score, MAX_ATTEMPTS, limit),
        ).fetchall()

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    # Convert rows to dicts
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d)...",
        len(jobs), min_score,
    )
    import time
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    for job in jobs:
        completed += 1
        try:
            letter = generate_cover_letter(resume_text, job, profile,
                                          validation_mode=validation_mode)

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
            cl_path.write_text(letter, encoding="utf-8")

            # Generate PDF (best-effort)
            pdf_path = None
            try:
                from applypilot.scoring.pdf import convert_to_pdf
                pdf_path = str(convert_to_pdf(cl_path))
            except Exception:
                log.debug("PDF generation failed for %s", cl_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": str(cl_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
            }
            results.append(result)

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [OK] | %.1f jobs/min | %s",
                completed, len(jobs), rate * 60, result["title"][:40],
            )
        except LLMCircuitOpenError as e:
            completed -= 1  # this job never actually got processed
            log.error("Aborting cover letters after %d/%d jobs -- %s", completed, len(jobs), e)
            break
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "path": None, "pdf_path": None, "error": str(e),
            }
            error_count += 1
            results.append(result)
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

    # Persist to DB: increment attempt counter for ALL, save path only for successes
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    for r in results:
        if r.get("path"):
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
            saved += 1
        else:
            conn.execute(
                "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)

    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
    }
