"""Cover letter generation for ApplyPilot."""

import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
from applypilot.database import get_connection
from applypilot.llm import get_client
from applypilot.scoring.validator import BANNED_WORDS, LLM_LEAK_PHRASES, sanitize_text

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5

# Common German job-posting phrases. If enough of these appear in the job
# text, the posting -- and therefore the cover letter -- is treated as German.
_GERMAN_MARKERS = [
    "wir suchen", "ihre aufgaben", "ihr profil", "wir bieten", "bewerbung",
    "kenntnisse", "unternehmen", "mitarbeiter", "aufgaben", "anforderungen",
    "sie verfügen", "unser team", "gmbh", "stellenangebot", "arbeitsort",
    "voraussetzungen", "berufserfahrung",
]


def detect_job_language(job: dict) -> str:
    """Return 'de' or 'en' based on the job posting's language (simple heuristic)."""
    text = f"{job.get('title', '')} {job.get('full_description') or ''}".lower()
    if not text.strip():
        return "en"
    marker_hits = sum(1 for m in _GERMAN_MARKERS if m in text)
    umlaut_hits = sum(text.count(ch) for ch in ("ä", "ö", "ü", "ß"))
    return "de" if (marker_hits >= 2 or umlaut_hits >= 5) else "en"


def _build_prompt(profile: dict, language: str = "en") -> str:
    if language == "de":
        return _build_prompt_de(profile)
    return _build_prompt_en(profile)


def _build_prompt_en(profile: dict) -> str:
    personal = profile.get("personal", {})
    facts = profile.get("resume_facts", {})
    auth = profile.get("work_authorization", {})

    name = personal.get("preferred_name") or personal.get("full_name", "")
    city = personal.get("city", "Zurich")
    permit = auth.get("work_permit_type", "B permit")
    companies = ", ".join(facts.get("preserved_companies", []))
    metrics = ", ".join(facts.get("real_metrics", []))
    school = facts.get("preserved_school", "")
    banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    return f"""You write cover letters for {name}, a project and product professional in {city}, Switzerland.

Here is a real example of a cover letter written in exactly the right style. Study it carefully and replicate the tone, structure, and logic in every letter you write:

--- EXAMPLE COVER LETTER ---
Dear Ergon Team,

I am applying for the IT Project Manager position. Ergon's model - end-to-end ownership of IT projects from requirements through delivery, in close collaboration with clients across financial services and public sector - is exactly the kind of role I am looking for, and I want to make a concrete case for why my background is relevant.

Project management in complex, cross-functional environments is the thread that runs through my entire career. At UBS I managed critical data and reporting workstreams across multiple stakeholders simultaneously, coordinated between business units and IT on time-sensitive delivery packages, and maintained governance and documentation standards under real regulatory accountability. During the UBS-Credit Suisse merger I was part of the team mapping over 10M in assets source-to-target - a project that required structured planning, dependency management, and rigorous quality control under significant pressure. At Swiss Re's Chief Innovation and Transformation Office I simultaneously supported three executives and a portfolio of divisional projects, designed and implemented a workforce tracking database that improved capacity planning by 70%, and ensured transparent status, risk, and cost reporting across teams. These are not simulated environments - they are demanding, accountable, client-facing roles in regulated financial institutions, which maps directly to Ergon's core client industries.

In my consulting projects at Berner Fachhochschule I led the full project lifecycle for real client engagements - from initial requirements analysis and stakeholder workshops through solution design, implementation support, and delivery. I structured workplans, managed dependencies and deadlines, coordinated cross-functional teams, and delivered complete outputs - technology roadmaps, process blueprints, financial plans, change management strategies - on time and to a standard the client could act on.

On the technical and methodological side, I am familiar with Scrum and Kanban from both corporate and consulting contexts, and I hold a Lean Six Sigma Green Belt reflecting a structured, methodology-grounded approach to planning and problem-solving. I hold a Master's in Digital Business Administration from Berner Fachhochschule and certifications in Microsoft Enterprise Product Management Fundamentals and Advanced Prompt Engineering. I am comfortable with project management tooling including Jira, Confluence, MS Project, and ServiceNow.

I speak German at an advanced level and am working actively towards native proficiency, and English at native level. I am based in {city}, hold a {permit}, am available immediately, and am fully willing to travel as required.

Ergon's culture - personal responsibility, open communication, working with like-minded people who care deeply about quality - is the kind of environment where I do my best work. I would very much welcome the opportunity to discuss my application.

Kind regards,
{name}
--- END EXAMPLE ---

Now write a new cover letter following the SAME style, tone, and structure as the example above, but tailored to the specific company and role in the job description provided.

RULES:
- Opening paragraph: name the specific company, describe what they actually do based on the job description, state why it fits. Be concrete, not generic.
- Experience paragraph(s): use real experience from {companies} with real metrics ({metrics}). Map the experience directly to what this specific role requires.
- Consulting/project paragraph: describe the full lifecycle - requirements, stakeholders, delivery, outputs. Connect to the job.
- Tools/credentials paragraph: only mention tools relevant to this specific job. Always include education from {school} and relevant certifications.
- Logistics paragraph: always include German (advanced, working towards native), English (native), location ({city}), permit ({permit}), available immediately.
- Closing paragraph: reference something specific about this company's culture or values from the job description. End with an invitation to speak.
- Sign off: Kind regards, {name}
- Length: 450-550 words
- Never invent facts, companies, degrees, metrics, or tools not in the resume
- Do not use: {banned}
- Do not include: {leak}
- No em dashes. Use commas or full stops.
- Start with "Dear [Company Name] Team," or "Dear Hiring Team," if no specific name available
- Output only the letter text. No subject line, no address block, no commentary."""


def _build_prompt_de(profile: dict) -> str:
    personal = profile.get("personal", {})
    facts = profile.get("resume_facts", {})
    auth = profile.get("work_authorization", {})

    name = personal.get("preferred_name") or personal.get("full_name", "")
    city = personal.get("city", "Zürich")
    permit = auth.get("work_permit_type", "B-Bewilligung")
    companies = ", ".join(facts.get("preserved_companies", []))
    metrics = ", ".join(facts.get("real_metrics", []))
    school = facts.get("preserved_school", "")
    banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    return f"""Du schreibst Bewerbungsschreiben für {name}, eine Projekt- und Produktexpertin in {city}, Schweiz. Die Stellenausschreibung ist auf Deutsch, daher muss das Bewerbungsschreiben auf professionellem, natürlichem Deutsch verfasst werden (fliessendes, korrektes Geschäftsdeutsch -- kein wörtlich übersetztes Englisch).

Hier ist ein echtes Beispiel eines Bewerbungsschreibens im genau richtigen Stil. Studiere Ton, Aufbau und Logik sorgfältig und übernimm sie in jedem Schreiben:

--- BEISPIEL-BEWERBUNGSSCHREIBEN ---
Sehr geehrtes Ergon-Team,

ich bewerbe mich für die Position als IT Project Manager. Das Modell von Ergon -- durchgängige Verantwortung für IT-Projekte von der Anforderungsaufnahme bis zur Auslieferung, in enger Zusammenarbeit mit Kunden aus dem Finanzsektor und dem öffentlichen Sektor -- entspricht genau der Art von Rolle, die ich suche, und ich möchte konkret darlegen, warum mein Hintergrund relevant ist.

Projektmanagement in komplexen, funktionsübergreifenden Umgebungen zieht sich durch meine gesamte Laufbahn. Bei der UBS habe ich kritische Daten- und Reporting-Workstreams über mehrere Stakeholder hinweg gleichzeitig gesteuert, zwischen Fachbereichen und IT bei zeitkritischen Arbeitspaketen koordiniert und Governance- sowie Dokumentationsstandards unter realer regulatorischer Verantwortung eingehalten. Während der Fusion von UBS und Credit Suisse war ich Teil des Teams, das über 10 Millionen an Vermögenswerten source-to-target abgebildet hat -- ein Projekt, das strukturierte Planung, Abhängigkeitsmanagement und strenge Qualitätskontrolle unter erheblichem Druck erforderte. Im Chief Innovation and Transformation Office der Swiss Re habe ich gleichzeitig drei Führungskräfte sowie ein Portfolio divisionaler Projekte unterstützt, eine Workforce-Tracking-Datenbank entworfen und implementiert, die die Kapazitätsplanung um 70% verbesserte, und für transparentes Status-, Risiko- und Kostenreporting über Teams hinweg gesorgt. Dies sind keine simulierten Umgebungen -- es sind anspruchsvolle, verantwortungsvolle, kundenorientierte Rollen in regulierten Finanzinstituten, was sich direkt mit den Kernbranchen von Ergon deckt.

In meinen Beratungsprojekten an der Berner Fachhochschule habe ich den gesamten Projektlebenszyklus für echte Kundenengagements geleitet -- von der initialen Anforderungsanalyse und Stakeholder-Workshops über das Lösungsdesign bis zur Umsetzungsunterstützung und Auslieferung. Ich habe Arbeitspläne strukturiert, Abhängigkeiten und Termine gemanagt, funktionsübergreifende Teams koordiniert und vollständige Ergebnisse -- Technologie-Roadmaps, Prozess-Blueprints, Finanzpläne, Change-Management-Strategien -- termingerecht und in einer für den Kunden umsetzbaren Qualität geliefert.

Auf der technischen und methodischen Seite bin ich mit Scrum und Kanban sowohl aus Konzern- als auch aus Beratungskontexten vertraut und besitze einen Lean Six Sigma Green Belt, der einen strukturierten, methodisch fundierten Ansatz für Planung und Problemlösung widerspiegelt. Ich habe einen Master in Digital Business Administration der Berner Fachhochschule sowie Zertifizierungen in Microsoft Enterprise Product Management Fundamentals und Advanced Prompt Engineering. Ich bin sicher im Umgang mit Projektmanagement-Tools wie Jira, Confluence, MS Project und ServiceNow.

Ich spreche Deutsch auf fortgeschrittenem Niveau und arbeite aktiv auf muttersprachliches Niveau hin, sowie Englisch auf muttersprachlichem Niveau. Ich bin in {city} wohnhaft, besitze eine {permit}, bin ab sofort verfügbar und uneingeschränkt reisebereit.

Die Kultur von Ergon -- Eigenverantwortung, offene Kommunikation, die Zusammenarbeit mit Gleichgesinnten, denen Qualität wirklich am Herzen liegt -- ist genau das Umfeld, in dem ich meine beste Arbeit leiste. Ich freue mich sehr auf die Gelegenheit, meine Bewerbung persönlich zu besprechen.

Freundliche Grüsse
{name}
--- ENDE BEISPIEL ---

Schreibe nun ein neues Bewerbungsschreiben im GLEICHEN Stil, Ton und Aufbau wie das Beispiel oben, aber zugeschnitten auf das konkrete Unternehmen und die Rolle aus der Stellenausschreibung.

REGELN:
- Einleitungsabsatz: Nenne das konkrete Unternehmen, beschreibe basierend auf der Stellenausschreibung, was es tatsächlich macht, und erkläre, warum die Rolle passt. Konkret, nicht generisch.
- Erfahrungsabsatz/-abschnitte: Nutze echte Erfahrung von {companies} mit echten Kennzahlen ({metrics}). Verbinde die Erfahrung direkt mit den Anforderungen dieser konkreten Rolle.
- Beratungs-/Projektabsatz: Beschreibe den vollständigen Lebenszyklus -- Anforderungen, Stakeholder, Lieferung, Ergebnisse. Verknüpfe mit der Stelle.
- Tools-/Qualifikationsabsatz: Erwähne nur Tools, die für diese konkrete Stelle relevant sind. Immer die Ausbildung von {school} und relevante Zertifizierungen erwähnen.
- Logistik-Absatz: Immer Deutsch (fortgeschritten, arbeitet auf Muttersprache hin), Englisch (Muttersprache), Wohnort ({city}), Bewilligung ({permit}), sofortige Verfügbarkeit erwähnen.
- Schlussabsatz: Beziehe dich auf etwas Konkretes zur Unternehmenskultur oder zu den Werten aus der Stellenausschreibung. Ende mit einer Einladung zum Gespräch.
- Grussformel: Freundliche Grüsse, {name}
- Länge: 450-550 Wörter
- Niemals Fakten, Unternehmen, Abschlüsse, Kennzahlen oder Tools erfinden, die nicht im Lebenslauf stehen
- Nicht verwenden (englische Füllwörter, die im Original-Prompt verboten sind -- falls im Deutschen ein direktes Äquivalent auftaucht, ebenfalls vermeiden): {banned}
- Nicht einschliessen: {leak}
- Keine Gedankenstriche (em dash). Kommas oder Punkte verwenden.
- Schweizer Rechtschreibung: "ss" statt "ß".
- Beginne mit "Sehr geehrtes [Unternehmen]-Team," oder "Sehr geehrte Damen und Herren," falls kein konkreter Name bekannt ist.
- Nur den Brieftext ausgeben. Keine Betreffzeile, keine Adresse, kein Kommentar."""


def generate_cover_letter(resume_text, job, profile, validation_mode="normal", language=None):
    if language is None:
        language = detect_job_language(job)
    system_prompt = _build_prompt(profile, language=language)
    job_text = (
        f"Job title: {job['title']}\n"
        f"Company: {job['site']}\n"
        f"Location: {job.get('location', 'Switzerland')}\n\n"
        f"Job description:\n{(job.get('full_description') or '')[:5000]}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB:\n{job_text}\n\nWrite the cover letter now."},
    ]
    client = get_client()
    raw = client.chat(messages, max_tokens=3000, temperature=0.6)
    letter = sanitize_text(raw).strip()
    log.info("Cover letter generated: %d chars", len(letter))
    return letter


def run_cover_letters(min_score=7, limit=20, validation_mode="normal"):
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = conn.execute(
        "SELECT * FROM jobs "
        "WHERE fit_score >= ? AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < ? "
        "ORDER BY fit_score DESC LIMIT ?",
        (min_score, MAX_ATTEMPTS, limit),
    ).fetchall()

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    if jobs and not isinstance(jobs[0], dict):
        columns = [d[0] for d in conn.execute("SELECT * FROM jobs LIMIT 0").description]
        jobs = [dict(zip(columns, row)) for row in jobs]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    results = []
    error_count = 0

    for i, job in enumerate(jobs, 1):
        try:
            letter = generate_cover_letter(resume_text, job, profile, validation_mode)

            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            cl_path = COVER_LETTER_DIR / f"{safe_site}_{safe_title}_CL.txt"
            cl_path.write_text(letter, encoding="utf-8")

            pdf_path = None
            try:
                from applypilot.scoring.pdf import convert_to_pdf
                pdf_path = str(convert_to_pdf(cl_path))
            except Exception:
                pass

            results.append({"url": job["url"], "path": str(cl_path), "pdf_path": pdf_path, "title": job["title"], "site": job["site"]})
            elapsed = time.time() - t0
            log.info("%d/%d [OK] %.1f jobs/min | %s", i, len(jobs), (i / elapsed) * 60 if elapsed else 0, job["title"][:50])

        except Exception as e:
            results.append({"url": job["url"], "title": job["title"], "site": job["site"], "path": None, "error": str(e)})
            error_count += 1
            log.error("%d/%d [ERROR] %s -- %s", i, len(jobs), job["title"][:50], e)

    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    for r in results:
        if r.get("path"):
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
            saved += 1
        else:
            conn.execute("UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?", (r["url"],))
    conn.commit()

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)
    return {"generated": saved, "errors": error_count, "elapsed": elapsed}