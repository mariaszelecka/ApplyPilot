# CV Tailoring Skill

This document is the source-of-truth spec for how ApplyPilot tailors Maria Szelecka's
resume per job. It exists so the tailoring rules are documented in one place, separate
from the code that enforces them. If you change a rule, update both this file and the
enforcement points listed under it.

Enforcement is layered: (1) the LLM prompt asks for it, (2) `validate_json_fields`
programmatically checks it and triggers a retry if violated, (3) the LLM judge
(`judge_tailored_resume`) double-checks for subtler violations, (4) where possible, the
rule is enforced in code instead of relying on the LLM at all.

**This file is live, not just documentation.** `tailor.py::_load_skill_hard_rules()`
reads the "## Hard Rules" section below from `~/.applypilot/CV_TAILORING_SKILL.md` at
runtime and splices it directly into the tailoring prompt. Editing the rules here
changes the next tailoring run with no code change needed -- but the copy in
`~/.applypilot/` (where the code actually reads from) must be kept in sync with this
repo copy; they are not automatically linked.

## Hard Rules

1. **Job titles never change.** The literal job title inside each EXPERIENCE header
   (e.g. "EA/Project Manager") must be copied verbatim -- no rewording, reordering, or
   translating. Only the resume's own overall title (top of page, e.g. "Business
   Analyst") is allowed to adapt to the target role.
   - Canonical list: `profile.json` -> `resume_facts.preserved_titles`
   - Enforced in: `tailor.py::_build_tailor_prompt` (instructs the LLM),
     `validator.py::validate_json_fields` (retries if a title is missing/altered),
     `tailor.py::_build_judge_prompt` (judge fails the resume if it catches a reworded title)

2. **No skill/keyword text under job titles.** The line under each EXPERIENCE or
   PROJECTS header is dates ONLY (e.g. "Jan 2022 - Oct 2022"). Never a tools/skills
   descriptor like "Process Automation, Data Analysis | Jan 2022 - Oct 2022".
   - Enforced by schema: the LLM's JSON has a `dates` field per entry, not a free-text
     `subtitle` -- there's no field to put keywords into.
   - Rendered in: `tailor.py::assemble_resume_text` (prints `entry["dates"]` as-is,
     nothing else, under each header)

3. **Education is always fully detailed and never LLM-generated.** Every tailored
   resume must show, for each degree, in this exact format (matching the reference
   "CV Maria Szelecka.pdf"):
   ```
   DEGREE NAME (uppercase)
   School, Location | Dates
   Modules: comma-separated list
   Thesis: thesis title            (only if the degree has one)
   GPA: x.xx                       (only if the degree has one)
   ERASMUS+: exchange details      (only if the degree has one)
   ```
   One block per degree, blank line between blocks, most recent first.
   - Source of truth: `profile.json` -> `resume_facts.education_history` (list of
     `{degree, school, location, dates, modules, thesis?, gpa?, erasmus?}`)
   - Rendered in: `tailor.py::assemble_resume_text` -- the LLM never sees or produces an
     "education" field at all; the EDUCATION section is built entirely from the profile
     so it can never be garbled, abbreviated, or fabricated.
   - PDF rendering: `pdf.py::parse_education_entries` + `build_html` (each degree its
     own block, with a `.edu-detail` line per Modules/Thesis/GPA/ERASMUS+ line --
     these must NOT be squashed into one paragraph).

4. **Every resume has a SKILLS section, not just TECHNICAL SKILLS.** A separate
   "SKILLS" section lists 5-8 relevant soft skills/competencies (e.g. "Stakeholder
   Management", "Business Analysis"), distinct from the "TECHNICAL SKILLS" section
   (tools/languages/frameworks).
   - Boundary list: `profile.json` -> `skills_boundary.competencies`
   - LLM output field: `competencies` (comma-separated string, top-level JSON key,
     sibling to `skills`)
   - Rendered in: `tailor.py::assemble_resume_text` (own "SKILLS" heading, before
     "TECHNICAL SKILLS") and `pdf.py::build_html` (own PDF section)

5. **No PROJECTS section, ever.** All real work -- including Capstone Projects and
   the Master's thesis -- lives under EXPERIENCE only. There is no separate
   "projects" field in the LLM's JSON and no PROJECTS heading in the output.
   - Enforced by schema: the JSON has no `projects` key at all.
   - Enforced in: `tailor.py::assemble_resume_text` (no PROJECTS block),
     `pdf.py::build_html` (no PROJECTS rendering), `validator.py` (no
     `projects` in required fields, no PROJECTS in required sections)

6. **The summary must never say "seasoned" or "seasoned professional".** This is a
   hard ban -- it applies in every validation mode (strict/normal/lenient), unlike
   the softer `BANNED_WORDS` list which is only a warning in "normal" mode.
   - List: `validator.py::HARD_BANNED_PHRASES`
   - Enforced in: `validate_json_fields`, `validate_tailored_resume`, and
     `validate_cover_letter` -- always an error, always triggers a retry.

## Pre-existing rules (unchanged, listed for completeness)

- **No fabrication.** Companies, degrees, certifications, and metrics must be real.
  Technical skills are bounded by `skills_boundary` (the LLM may add 2-3 closely
  related tools, nothing from an unrelated stack). See `FABRICATION_WATCHLIST` in
  `validator.py`.
- **Preserved companies.** Every company in `resume_facts.preserved_companies` must
  appear somewhere in EXPERIENCE. An entry may be a list of acceptable
  name variants (e.g. `["Credit Suisse", "UBS-CS", "UBS–CS", "UBS/CS"]`) when the
  original resume only ever refers to it by an abbreviation.
- **No banned filler words** (e.g. "passionate", "synergy", "cutting-edge") -- see
  `BANNED_WORDS` in `validator.py`. Severity depends on `validation_mode`
  (strict/normal/lenient).
- **No em dashes.** Auto-fixed by `sanitize_text`, double-checked by the validator.
- **Real metrics only, never inflated.** See `resume_facts.real_metrics`.
- **One page.**

## Tailored resume JSON schema (what the LLM returns)

```json
{
  "title": "Role Title",
  "summary": "2-3 tailored sentences.",
  "competencies": "Competency 1, Competency 2, Competency 3, Competency 4, Competency 5",
  "skills": {"Languages": "...", "Frameworks": "...", "Tools": "..."},
  "experience": [
    {"header": "Title at Company", "dates": "Mon YYYY - Mon YYYY", "bullets": ["...", "..."]}
  ]
}
```

Note there is no `"education"` key (code-injected after assembly, not requested from
the LLM) and no `"projects"` key (no PROJECTS section at all).

## Where this is implemented

| Concern | File |
|---|---|
| Prompt construction | `src/applypilot/scoring/tailor.py::_build_tailor_prompt` |
| Judge prompt | `src/applypilot/scoring/tailor.py::_build_judge_prompt` |
| Text assembly (header/education/skills injection) | `src/applypilot/scoring/tailor.py::assemble_resume_text` |
| Programmatic validation | `src/applypilot/scoring/validator.py::validate_json_fields` |
| PDF rendering | `src/applypilot/scoring/pdf.py::build_html` |
| User data (titles/education/competencies) | `~/.applypilot/profile.json` -> `resume_facts`, `skills_boundary` |

This file must be kept in sync in both places the package lives during local
development: the repo (`ApplyPilot/src/applypilot/...`) and the installed copy
(`site-packages/applypilot/...`), until the package is reinstalled from the repo.
