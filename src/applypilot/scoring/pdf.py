"""Text-to-PDF conversion for tailored resumes and cover letters.

Renders the exact structured text format tailor.py / cover_letter.py produce
into HTML, then exports to PDF using headless Chromium via Playwright.
Resumes and cover letters are detected and rendered by two different paths
(a cover letter has no section headers at all -- treating it as a resume
silently drops most of its content).
"""

import base64
import logging
import mimetypes
import re
from pathlib import Path

from applypilot.config import COVER_LETTER_DIR, TAILORED_DIR

log = logging.getLogger(__name__)


def _load_photo_data_uri() -> str | None:
    """Load the candidate's headshot from profile.json -> personal.photo_path
    (if configured) as a base64 data: URI for embedding in the PDF. Returns
    None if not configured or the file doesn't exist -- the header still
    renders cleanly without a photo either way."""
    try:
        from applypilot.config import load_profile
        photo_path = load_profile().get("personal", {}).get("photo_path")
    except Exception:
        return None
    if not photo_path:
        return None
    path = Path(photo_path)
    if not path.exists():
        log.warning("Configured photo_path does not exist: %s", path)
        return None
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"

_BULLET_RE = re.compile(r"^[•\-\*]\s*")


def _is_bullet(line: str) -> bool:
    return bool(_BULLET_RE.match(line.strip()))


def _strip_bullet(line: str) -> str:
    return _BULLET_RE.sub("", line.strip()).strip()


def is_cover_letter(text: str, path: Path | None = None) -> bool:
    """Cover letters are always written to `*_CL.txt` (see cover_letter.py) --
    the reliable signal, since the letter now opens with the same name+
    contact header a resume does. Falls back to the old "starts with Dear"
    heuristic for text handed in without a path, or pre-letterhead files."""
    if path is not None and path.stem.endswith("_CL"):
        return True
    return text.strip().lower().startswith("dear")


# ── Resume parsing ───────────────────────────────────────────────────────

_RESUME_SECTION_NAMES = (
    "PROFESSIONAL SUMMARY", "SUMMARY", "PROFESSIONAL EXPERIENCE", "EXPERIENCE",
    "PROJECTS", "EDUCATION", "SKILLS", "SOFTWARE SKILLS", "TECHNICAL SKILLS",
    "LANGUAGES", "CERTIFICATIONS",
)


def parse_resume(text: str) -> dict:
    """Parse the resume text format produced by tailor.py.

    Header: name (line 1), pipe-separated contact line (line 2).
    Body: split into sections by ALL-CAPS headers matching
    _RESUME_SECTION_NAMES.

    Returns:
        {"name": str, "contact": str, "sections": {header: raw_text}}
    """
    lines = [line.rstrip() for line in text.strip("\n").split("\n")]

    name = lines[0].strip() if len(lines) > 0 else ""
    role_title = lines[1].strip() if len(lines) > 1 else ""
    contact = lines[2].strip() if len(lines) > 2 else ""

    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[3:]:
        stripped = line.strip()
        if stripped.upper() in _RESUME_SECTION_NAMES:
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped.upper()
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {"name": name, "role_title": role_title, "contact": contact, "sections": sections}


def parse_contact_line(contact: str) -> dict[str, str]:
    """Split a 'Label: value | Label: value | ...' contact line into a dict."""
    fields: dict[str, str] = {}
    for part in contact.split("|"):
        part = part.strip()
        if ":" in part:
            label, value = part.split(":", 1)
            fields[label.strip()] = value.strip()
    return fields


def parse_experience_block(text: str) -> list[dict]:
    """Parse a PROFESSIONAL EXPERIENCE block into entries: title, company_line,
    job_scope bullets, achievements bullets."""
    entries: list[dict] = []
    current: dict | None = None
    in_achievements = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_bullet(line):
            if current is None:
                continue
            bullet = _strip_bullet(line)
            (current["achievements"] if in_achievements else current["job_scope"]).append(bullet)
        elif line.upper().rstrip(":") == "ACHIEVEMENTS":
            in_achievements = True
        elif current is None or current["company_line"]:
            if current:
                entries.append(current)
            current = {"title": line, "company_line": "", "job_scope": [], "achievements": []}
            in_achievements = False
        else:
            current["company_line"] = line

    if current:
        entries.append(current)
    return entries


def parse_simple_entries(text: str) -> list[dict]:
    """Parse a PROJECTS-style block: a title line followed by bullets,
    repeated, with no achievements sub-label."""
    entries: list[dict] = []
    current: dict | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_bullet(line):
            if current is None:
                continue
            current["bullets"].append(_strip_bullet(line))
        else:
            if current:
                entries.append(current)
            current = {"title": line, "bullets": []}

    if current:
        entries.append(current)
    return entries


_EDU_FIELD_PREFIXES = ("GPA:", "THESIS:", "INTERNATIONAL EXCHANGE")


def render_education_html(text: str) -> str:
    """Render the verbatim EDUCATION block. Structure per degree is always
    Degree / School+Location+Dates / optional GPA,Thesis,exchange-bullets --
    tracked with a small state machine rather than trying to regex-match
    arbitrary formatting."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    parts: list[str] = []
    state = "degree"

    for line in lines:
        if _is_bullet(line):
            parts.append(f'<div class="edu-detail">{_strip_bullet(line)}</div>')
            continue
        upper = line.upper()
        if upper.startswith(_EDU_FIELD_PREFIXES):
            parts.append(f'<div class="edu-detail">{line}</div>')
            state = "details"
            continue
        if state == "degree":
            parts.append(f'<div class="edu-degree">{line}</div>')
            state = "school"
        elif state == "school":
            parts.append(f'<div class="edu-school">{line}</div>')
            state = "details"
        else:
            # Non-field line while expecting details -- must be the next degree.
            parts.append(f'<div class="edu-degree">{line}</div>')
            state = "school"

    return "".join(parts)


def build_resume_html(resume: dict, photo_data_uri: str | None = None) -> str:
    """Build resume HTML matching the reference CV layout: name + target
    role title, a two-column contact block (Email/Phone/Location left,
    clickable LinkedIn/GitHub icon-links right, optional circular photo),
    bold job titles with italic company/date lines, a plain (non-bold)
    'Achievements:' sub-label, and verbatim-rendered Education/Software
    Skills/Languages/Certifications. A thin rule appears only directly under
    the header and each section title -- nowhere else.
    """
    sections = resume["sections"]

    def section(name: str, inner: str) -> str:
        return f'<div class="section"><div class="section-title">{name}</div>{inner}</div>'

    summary_html = ""
    summary_text = sections.get("PROFESSIONAL SUMMARY") or sections.get("SUMMARY")
    if summary_text:
        summary_html = section("Professional Summary", f'<div class="summary">{summary_text}</div>')

    exp_html = ""
    exp_text = sections.get("PROFESSIONAL EXPERIENCE") or sections.get("EXPERIENCE")
    if exp_text:
        items = ""
        for e in parse_experience_block(exp_text):
            job_scope = "".join(f"<li>{b}</li>" for b in e["job_scope"])
            achievements = ""
            if e["achievements"]:
                ach_items = "".join(f"<li>{b}</li>" for b in e["achievements"])
                achievements = f'<div class="ach-label">Achievements:</div><ul>{ach_items}</ul>'
            items += (
                f'<div class="entry">'
                f'<div class="entry-title">{e["title"]}</div>'
                f'<div class="entry-subtitle">{e["company_line"]}</div>'
                f'<ul>{job_scope}</ul>{achievements}'
                f'</div>'
            )
        exp_html = section("Experience", items)

    proj_html = ""
    proj_text = sections.get("PROJECTS")
    if proj_text:
        items = ""
        for e in parse_simple_entries(proj_text):
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div><ul>{bullets}</ul></div>'
        proj_html = section("Projects", items)

    edu_html = ""
    edu_text = sections.get("EDUCATION")
    if edu_text:
        edu_html = section("Education", render_education_html(edu_text))

    skills_html = ""
    skills_text = sections.get("SKILLS")
    if skills_text:
        skills_html = section("Skills", f'<div class="pipe-list">{skills_text}</div>')

    software_html = ""
    software_text = sections.get("SOFTWARE SKILLS") or sections.get("TECHNICAL SKILLS")
    if software_text:
        software_html = section("Software Skills", f'<div class="pipe-list">{software_text}</div>')

    languages_html = ""
    languages_text = sections.get("LANGUAGES")
    if languages_text:
        items = "".join(f"<li>{_strip_bullet(l)}</li>" for l in languages_text.splitlines() if l.strip())
        languages_html = section("Languages", f"<ul>{items}</ul>")

    certifications_html = ""
    cert_text = sections.get("CERTIFICATIONS")
    if cert_text:
        items = "".join(f"<li>{_strip_bullet(l)}</li>" for l in cert_text.splitlines() if l.strip())
        certifications_html = section("Certifications", f"<ul>{items}</ul>")

    header_html = build_header_html(resume["name"], resume["role_title"], resume["contact"], photo_data_uri)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
{_RESUME_CSS}
</style>
</head>
<body>
{header_html}
{summary_html}
{exp_html}
{proj_html}
{edu_html}
{skills_html}
{software_html}
{languages_html}
{certifications_html}
</body>
</html>"""


def _pretty_url(url: str) -> str:
    """Strip the scheme/www for display text, keep the full URL as the href."""
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


_LINKEDIN_SVG = (
    '<svg viewBox="0 0 24 24" width="13" height="13" fill="#0a66c2">'
    '<path d="M20.45 20.45h-3.55v-5.57c0-1.33-.02-3.03-1.85-3.03-1.85 0-2.14 1.45-2.14 2.94v5.66H9.36V9h3.41v1.56h.05'
    'c.48-.9 1.64-1.85 3.38-1.85 3.61 0 4.28 2.38 4.28 5.47v6.27zM5.34 7.43a2.06 2.06 0 1 1 0-4.12 2.06 2.06 0 0 1 0 4.12z'
    'M7.12 20.45H3.56V9h3.56v11.45z"/></svg>'
)
_GITHUB_SVG = (
    '<svg viewBox="0 0 24 24" width="13" height="13" fill="#333">'
    '<path d="M12 .5C5.65.5.5 5.65.5 12c0 5.09 3.29 9.4 7.86 10.93.57.1.79-.25.79-.55 0-.27-.01-1.16-.02-2.11'
    'c-3.2.7-3.87-1.36-3.87-1.36-.53-1.34-1.29-1.7-1.29-1.7-1.05-.72.08-.7.08-.7 1.17.08 1.78 1.2 1.78 1.2 1.03 1.77 '
    '2.71 1.26 3.37.96.1-.75.4-1.26.73-1.55-2.55-.29-5.24-1.28-5.24-5.68 0-1.25.45-2.28 1.19-3.08-.12-.29-.52-1.46.11'
    '-3.05 0 0 .97-.31 3.18 1.18a11 11 0 0 1 5.79 0c2.2-1.49 3.17-1.18 3.17-1.18.64 1.59.24 2.76.12 3.05.74.8 1.19 '
    '1.83 1.19 3.08 0 4.41-2.69 5.38-5.25 5.67.41.36.78 1.06.78 2.15 0 1.55-.01 2.8-.01 3.18 0 .3.21.66.8.55A11.5 '
    '11.5 0 0 0 23.5 12c0-6.35-5.15-11.5-11.5-11.5z"/></svg>'
)


def build_header_html(name: str, role_title: str, contact: str, photo_data_uri: str | None = None) -> str:
    """Two-column contact block: Email/Phone/Location on the left,
    LinkedIn/GitHub as clickable icon links on the right. Optional circular
    photo to the left of the contact block."""
    fields = parse_contact_line(contact)

    left_rows = ""
    for label in ("Email", "Phone", "Location"):
        if label not in fields:
            continue
        value = fields[label]
        if label == "Email":
            value_html = f'<a href="mailto:{value}">{value}</a>'
        else:
            value_html = value
        left_rows += f'<div class="contact-row"><span class="contact-label">{label}:</span> {value_html}</div>'

    right_rows = ""
    if "LinkedIn" in fields:
        url = fields["LinkedIn"]
        right_rows += f'<div class="contact-row">{_LINKEDIN_SVG} <a href="{url}">{_pretty_url(url)}</a></div>'
    if "GitHub" in fields:
        url = fields["GitHub"]
        right_rows += f'<div class="contact-row">{_GITHUB_SVG} <a href="{url}">{_pretty_url(url)}</a></div>'

    photo_html = f'<img class="photo" src="{photo_data_uri}">' if photo_data_uri else ""
    role_html = f'<div class="role-title">{role_title}</div>' if role_title else ""

    return f"""<div class="header">
    <div class="name">{name}</div>
    {role_html}
    <div class="header-rule"></div>
    <div class="contact-block">
        {photo_html}
        <div class="contact-col">{left_rows}</div>
        <div class="contact-col">{right_rows}</div>
    </div>
</div>"""


_RESUME_CSS = """
@page { size: letter; margin: 0.4in 0.55in; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Calibri', 'Segoe UI', Arial, sans-serif; font-size: 10pt; line-height: 1.35; color: #1a1a1a; }

/* Header -- a rule appears ONLY directly under the name/role block, nothing else in the header */
.header { text-align: left; margin-bottom: 10px; }
.name { font-size: 24pt; font-weight: 800; color: #111; letter-spacing: 0.3px; }
.role-title { font-size: 11pt; color: #444; margin-top: 2px; }
.header-rule { border-bottom: 1px solid #ccc; margin: 10px 0; }
.contact-block { display: flex; align-items: flex-start; gap: 24px; }
.photo { width: 72px; height: 72px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
.contact-col { display: flex; flex-direction: column; gap: 4px; font-size: 9.5pt; }
.contact-row { display: flex; align-items: center; gap: 5px; }
.contact-label { font-weight: 700; color: #111; }
.contact-row a { color: #0a66c2; text-decoration: none; }
.contact-row svg { flex-shrink: 0; }

/* Sections -- a rule appears ONLY directly under the section title, nowhere else */
.section { margin-top: 10px; break-inside: avoid; }
.section-title { font-size: 12pt; font-weight: 700; color: #111; padding-bottom: 3px;
    margin-bottom: 6px; border-bottom: 1px solid #333; }
.summary { font-size: 9.5pt; color: #333; line-height: 1.4; }

.entry { margin-bottom: 8px; break-inside: avoid; }
.entry-title { font-weight: 700; font-size: 10pt; color: #111; }
.entry-subtitle { font-size: 9pt; color: #444; font-style: italic; margin-bottom: 2px; }
.ach-label { font-size: 9.5pt; font-weight: 400; color: #111; margin-top: 2px; }

ul { margin-left: 16px; padding: 0; }
li { font-size: 9.5pt; margin-bottom: 1px; line-height: 1.35; }

.edu-degree { font-weight: 700; font-size: 10pt; color: #111; margin-top: 4px; }
.edu-school { font-size: 9.5pt; font-style: italic; color: #444; }
.edu-detail { font-size: 9.5pt; color: #333; }
.pipe-list { font-size: 9.5pt; line-height: 1.5; }
"""


# ── Cover letter parsing ─────────────────────────────────────────────────

def parse_cover_letter(text: str) -> dict | None:
    """Parse the letterhead cover letter format produced by
    cover_letter.py::assemble_cover_letter_text:

        Name
        contact line

        Dear Company Team,

        <body paragraphs...>

        Kind regards,

        Name

    Returns None if the text doesn't match this structure (older letters
    with the fuller date/company/subject-line letterhead) so the caller can
    fall back to a plain render.
    """
    blocks = [b.strip() for b in text.strip().split("\n\n")]
    if len(blocks) < 5:
        return None

    header_lines = blocks[0].splitlines()
    salutation = blocks[1]
    if not salutation.lower().startswith("dear "):
        return None

    body_blocks = blocks[2:-2]
    if not body_blocks:
        return None

    return {
        "name": header_lines[0].strip() if header_lines else "",
        "contact": header_lines[1].strip() if len(header_lines) > 1 else "",
        "salutation": salutation,
        "body_paragraphs": body_blocks,
        "signoff_label": blocks[-2],
        "signature_name": blocks[-1],
    }


def build_cover_letter_html(text: str) -> str:
    """Render a cover letter matching the reference style: bold name +
    contact + thin rule (matching the resume header), the salutation, body
    paragraphs, and a plain 'Kind regards, / Name' sign-off -- no date,
    company block, or subject line, and nothing after the name in the
    sign-off. Falls back to plain paragraph rendering for older letters
    that predate this format."""
    parsed = parse_cover_letter(text)

    if parsed is None:
        paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
        body_html = "".join(f"<p>{p}</p>" for p in paragraphs)
        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{ size: letter; margin: 0.75in 0.9in; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Calibri', 'Segoe UI', Arial, sans-serif; font-size: 11pt; line-height: 1.5; color: #1a1a1a; }}
p {{ margin-bottom: 12px; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""

    body_html = "".join(f"<p>{p}</p>" for p in parsed["body_paragraphs"])

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="format-detection" content="telephone=no, email=no, address=no">
<style>
@page {{ size: letter; margin: 0.5in 0.65in; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; color: inherit; }}
body {{ font-family: 'Calibri', 'Segoe UI', Arial, sans-serif; font-size: 10.5pt; line-height: 1.5; color: #1a1a1a; }}
.name {{ font-size: 22pt; font-weight: 800; color: #111 !important; letter-spacing: 0.3px; }}
.contact {{ font-size: 9.5pt; color: #444 !important; margin-top: 4px; }}
.header-rule {{ border-bottom: 1px solid #ccc; margin: 10px 0 20px; }}
.salutation {{ color: #1a1a1a !important; margin-bottom: 12px; }}
p {{ color: #1a1a1a !important; margin-bottom: 12px; text-align: justify; }}
.signoff-label {{ color: #1a1a1a !important; margin-top: 4px; margin-bottom: 4px; }}
.signature-name {{ font-weight: 700; color: #111 !important; }}
</style>
</head>
<body>
<div class="name">{parsed["name"]}</div>
<div class="contact">{parsed["contact"]}</div>
<div class="header-rule"></div>
<div class="salutation">{parsed["salutation"]}</div>
{body_html}
<div class="signoff-label">{parsed["signoff_label"]}</div>
<div class="signature-name">{parsed["signature_name"]}</div>
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Detects which one it is (cover letters start with "Dear" and have no
    section headers -- treating one as a resume silently drops its content)
    and renders with the matching template.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")

    if is_cover_letter(text, path=text_path):
        html = build_cover_letter_html(text)
    else:
        resume = parse_resume(text)
        html = build_resume_html(resume, photo_data_uri=_load_photo_data_uri())

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR and COVER_LETTER_DIR that don't
    have corresponding PDFs.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    candidates: list[Path] = []
    for d in (TAILORED_DIR, COVER_LETTER_DIR):
        if not d.exists():
            continue
        candidates.extend(f for f in sorted(d.glob("*.txt")) if not f.name.endswith("_JOB.txt"))

    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            convert_to_pdf(f)
            converted += 1
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d PDFs generated", converted, len(to_convert))
    return converted
