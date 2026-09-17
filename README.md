<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

> **This fork** ([mariaszelecka/ApplyPilot](https://github.com/mariaszelecka/ApplyPilot)) is maintained independently from the original, with substantial changes: an explicit approval gate (nothing is tailored or submitted without an email reply naming the job), region-based match filtering, a single-email digest-and-approve flow, and a round of reliability and security hardening. See commit history for the full set of changes.

# ApplyPilot

**Discovers and scores jobs autonomously. Tailors and applies only to what you approve. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)

---

## What It Does

ApplyPilot runs in two phases:

**1. Discovery & Digest (automated, runs daily on its own).** Scrapes 5+ job boards, scores every posting against your resume with an LLM, and sends you one digest email with the matches. Nothing is written or submitted anywhere at this stage — it's read-only.

**2. Human-in-the-loop (only after you reply).** You reply to the digest naming the job numbers you actually want. That reply is the trigger: `apply_status` for those jobs flips to `approved`, and only then does ApplyPilot generate a tailored CV and cover letter, drive a browser through the application form (uploads, screening questions, submit), and send you a confirmation email with the outcome. Jobs you didn't name stay untouched — no CV, no cover letter, no application.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot daily         # discover > score > email one digest — tailors/submits ONLY what you approved last time
applypilot poll          # run every ~15 min: check for a reply, tailor + submit only the jobs you named
```

> Reply to the digest email with the job numbers you want (e.g. "2, 5, 7" or "all"). `applypilot poll` (or the next `applypilot daily`) picks up the approval, tailors a CV + cover letter for exactly those jobs, and submits them.

`applypilot run` and `applypilot apply` also exist as lower-level, manual commands for running individual stages or a specific URL by hand — see [CLI Reference](#cli-reference). They're useful for testing, but `daily` + `poll` are the actual approval-gated process described above.

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs make the digest |
| **4. Digest & Approve** | One email a day with every new match. **Nothing further happens until you reply** with the numbers of the jobs you want — that reply is the only approval ApplyPilot ever acts on |
| **5. Tailor** | For jobs you named only: AI rewrites your resume per job — reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **6. Cover Letter** | For jobs you named only: AI generates a targeted cover letter |
| **7. Auto-Apply** | For jobs you named only: Claude Code navigates the application form, fills fields, uploads documents, answers questions, and submits |

Stages 1-3 run automatically every day regardless of approval — that's just discovery and scoring, nothing is sent anywhere external. Stages 5-7 run *only* for jobs you explicitly named in a digest reply; this is enforced at the database level (see [How Stages Work](#how-stages-work)), not just by prompt instructions.

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional).

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold make the digest — nothing is tailored yet.

### Digest & Approve
`applypilot daily` emails you one digest listing every new match above your score threshold, numbered. You reply to that email with the numbers you want (e.g. "2, 5, 7" or "all"). That reply is read over IMAP and is the *only* thing that moves a job from "matched" to `apply_status='approved'` in the database — nothing is tailored or submitted for a job you didn't name, and this is enforced by the query that selects jobs for tailoring and submission, not just by instructing the AI to behave.

Run `applypilot poll` on a short interval (every ~15 min) alongside the once-a-day `daily` job to act on a reply within minutes instead of waiting for tomorrow's run — it does no new discovery or scoring, only tailoring + submission for jobs you've already approved.

### Tailor
For every job you approved that doesn't have one yet: generates a custom resume, reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
For the same approved jobs: writes a targeted cover letter referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
For the same approved jobs: Claude Code launches a Chrome instance, navigates to the application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time. If a form hits a CAPTCHA or a login wall it doesn't have credentials for, it stops and hands the job back to you rather than guessing.

The Playwright MCP server is configured automatically at runtime per worker, scoped so the agent can only reach that server and nothing else on your machine. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## Security & Guardrails

The apply agent reads untrusted text (job postings) and drives a real browser with your real accounts, so it's scoped down on purpose:

| Guardrail | What it does |
|-----------|---------------|
| **Approval gate** | Enforced in the SQL query itself (`acquire_job()`), not just in the prompt — a live run can only pick up jobs with `apply_status='approved'`, which is only ever set by your digest reply or an explicit `applypilot apply --approve`. |
| **MCP scope isolation** | The apply agent runs with `--strict-mcp-config`, so it can only reach the two MCP servers it's actually given (Playwright + Gmail) — not whatever else happens to be configured on your machine (Notion, Calendar, Drive, etc). |
| **No outbound email tool** | `mcp__gmail__send_email` and every other Gmail write/send tool is in `--disallowedTools`. An agent that reads arbitrary job-posting text should never also be able to send mail — that combination is a data-exfiltration path, so it's blocked outright rather than trusted to behave. |
| **Stop-on-uncertainty** | CAPTCHAs and unfamiliar login walls make the agent stop and hand the job back to you instead of guessing or trying to work around them — see `RESULT:CAPTCHA` / `RESULT:FAILED:login_issue` in the transcripts. |
| **Kill switch** | `Ctrl+C` once skips the job currently in progress; `Ctrl+C` twice kills every active Claude/Chrome process tree and stops the run immediately. |
| **Dry-run mode** | `applypilot apply --dry-run` (or `applypilot daily --no-live-apply`) fills and screenshots forms without ever clicking submit, so you can see exactly what would be sent before it is. |
| **Dependency pinning** | `requirements-lock.txt` pins exact versions of every production dependency, so `pip install` can't silently pull in a newer, unreviewed release. |

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot daily                        # THE main command: discover > score > digest email,
                                         #   then tailor + cover letter + submit whatever you
                                         #   approved by replying to the previous digest
applypilot daily --no-email             # Same, but skip sending the digest (still applies approvals)
applypilot daily --no-live-apply        # Tailor approved jobs but don't submit yet
applypilot poll                         # Check for a digest reply and act on it now, without
                                         #   waiting for tomorrow's daily run (no discovery/scoring)
applypilot poll --no-apply              # Tailor newly-approved jobs but don't submit
applypilot run [stages...]              # Manual/testing: run pipeline stages directly (or 'all'),
                                         #   bypassing the digest-approval gate for tailoring
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply (only ever submits apply_status='approved' jobs)
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
```

> `applypilot run`'s `tailor`/`cover` stages work directly off the score threshold and are **not** gated by a digest reply — they're a manual/debugging primitive, not the approval flow. Submission (`applypilot apply`) is gated regardless of how a job got tailored: it only ever submits jobs with `apply_status='approved'`, and that status is set only by a digest reply or an explicit `applypilot apply --approve`.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
