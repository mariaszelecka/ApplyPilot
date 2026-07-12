"""Text-to-PDF conversion for tailored resumes and cover letters."""
import logging
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from applypilot.config import TAILORED_DIR
except Exception:
    TAILORED_DIR = Path.home() / ".applypilot" / "tailored_resumes"

# Exact section headings assemble_resume_text() emits. Body content (e.g. an
# all-caps degree name like "MSC. INTERNATIONAL BUSINESS", or a colon-free
# all-caps line like "GPA: 6.00") must never be mistaken for a new section --
# so section boundaries are matched against this whitelist, not a heuristic.
KNOWN_SECTIONS = {"SUMMARY", "SKILLS", "TECHNICAL SKILLS", "EXPERIENCE", "EDUCATION"}


def parse_resume(text: str) -> dict:
    lines = [line.rstrip() for line in text.strip().split("\n")]
    header_lines = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())
    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]
    sections = {}
    current_section = None
    current_lines = []
    for line in lines[body_start:]:
        stripped = line.strip()
        if stripped in KNOWN_SECTIONS:
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)
    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()
    return {"name": name, "title": title, "location": location, "contact": contact, "sections": sections}


def parse_skills(text: str) -> list:
    skills = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list:
    entries = []
    lines = text.strip().split("\n")
    current = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (not stripped.startswith("-") and not stripped.startswith("\u2022") and len(current.get("bullets", [])) > 0):
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)
    if current:
        entries.append(current)
    return entries


def parse_education_entries(text: str) -> list:
    """Split the EDUCATION section into per-degree blocks (blank-line separated).

    Each block: degree name (line 1), school/location/dates (line 2),
    then detail lines (Modules/Thesis/GPA/ERASMUS+), in that order.
    """
    blocks = [b.strip() for b in text.strip().split("\n\n") if b.strip()]
    entries = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        degree = lines[0]
        subtitle = lines[1] if len(lines) > 1 else ""
        details = lines[2:]
        entries.append({"degree": degree, "subtitle": subtitle, "details": details})
    return entries


def build_html(resume: dict) -> str:
    sections = resume["sections"]
    competencies_html = ""
    if "SKILLS" in sections:
        competencies_html = f'<div class="section"><div class="section-title">Skills</div><div class="summary">{sections["SKILLS"].strip()}</div></div>'
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'
    edu_html = ""
    if "EDUCATION" in sections:
        edu_entries = parse_education_entries(sections["EDUCATION"])
        items = ""
        for e in edu_entries:
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            details = "".join(f'<div class="edu-detail">{d}</div>' for d in e["details"])
            items += f'<div class="entry"><div class="entry-title">{e["degree"]}</div>{subtitle}{details}</div>'
        edu_html = f'<div class="section"><div class="section-title">Education</div>{items}</div>'
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{sections["SUMMARY"].strip()}</div></div>'
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{ size: letter; margin: 0.35in 0.5in; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Calibri', 'Segoe UI', Arial, sans-serif; font-size: 10pt; line-height: 1.35; color: #1a1a1a; }}
.header {{ text-align: center; margin-bottom: 4px; padding-bottom: 4px; border-bottom: 1.5px solid #2a7ab5; }}
.name {{ font-size: 18pt; font-weight: 700; color: #1a3a5c; letter-spacing: 0.5px; }}
.title {{ font-size: 10.5pt; color: #3a6b8c; margin: 1px 0; }}
.location {{ font-size: 9pt; color: #555; }}
.contact {{ font-size: 9pt; color: #444; margin-top: 1px; }}
.section {{ margin-top: 5px; }}
.section-title {{ font-size: 10pt; font-weight: 700; color: #1a3a5c; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1.5px solid #2a7ab5; padding-bottom: 1px; margin-bottom: 3px; }}
.summary {{ font-size: 9.5pt; color: #333; line-height: 1.4; }}
.skill-row {{ font-size: 9.5pt; margin: 0; line-height: 1.35; }}
.skill-cat {{ font-weight: 600; color: #1a3a5c; }}
.entry {{ margin-bottom: 4px; break-inside: avoid; }}
.entry-title {{ font-weight: 600; font-size: 10pt; color: #1a3a5c; }}
.entry-subtitle {{ font-size: 9pt; color: #4a7a9b; font-style: italic; margin-bottom: 1px; }}
ul {{ margin-left: 14px; padding: 0; }}
li {{ font-size: 9.5pt; margin-bottom: 1px; line-height: 1.35; }}
.edu {{ font-size: 10pt; }}
.edu-detail {{ font-size: 9.5pt; color: #333; margin-bottom: 1px; line-height: 1.35; }}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    <div class="title">{resume['title']}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{competencies_html}
{skills_html}
{exp_html}
{edu_html}
</body>
</html>"""


def build_cover_letter_html(text: str) -> str:
    paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    body = ""
    for p in paragraphs:
        lines = p.split("\n")
        body += "<p>" + "<br>".join(lines) + "</p>\n"
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{ size: A4; margin: 2.5cm 2.5cm; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: Calibri, Arial, sans-serif; font-size: 11pt; line-height: 1.6; color: #1a1a1a; }}
p {{ margin-bottom: 12pt; text-align: left; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def render_pdf(html: str, output_path: str) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="A4",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


def convert_to_pdf(text_path, output_path=None, html_only=False):
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    if "_CL" in text_path.name:
        html = build_cover_letter_html(text)
    else:
        resume = parse_resume(text)
        html = build_html(resume)
    if html_only:
        out = Path(output_path or text_path.with_suffix(".html"))
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out
    out = Path(output_path or text_path.with_suffix(".pdf"))
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit=50):
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0
    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    candidates = [f for f in txt_files if not f.name.endswith("_JOB.txt")]
    to_convert = []
    for f in candidates:
        if not f.with_suffix(".pdf").exists():
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
    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
