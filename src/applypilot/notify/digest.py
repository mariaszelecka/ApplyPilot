"""Daily digest email: the first approval gate.

Flow: search -> match -> email -> you approve or not -> ONLY THEN does
ApplyPilot generate a tailored CV + cover letter and move toward applying.
This module emails the digest right after scoring -- job title, company,
fit score, why it matched, and a link -- with no CV/cover letter attached
yet, since none has been generated. Nothing gets tailored, previewed, or
submitted for a job you didn't approve here first (see
check_email_approvals(), which reads your reply and is what actually
triggers CV/cover-letter generation for the jobs you named).
"""

import email as email_lib
import imaplib
import json
import logging
import os
import re
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from applypilot.config import load_env, load_profile
from applypilot.database import get_connection

log = logging.getLogger(__name__)


def _pending_digest_jobs(conn, min_score: int, limit: int) -> list[dict]:
    """Jobs ready for the FIRST approval gate: scored, not yet included in a
    previous digest, not already applied to, freshly discovered, and in an
    active region bucket (currently Switzerland only -- see
    scoring.region_quota, still paused for Dublin/France/US).

    No longer requires tailored_resume_path/cover_letter_path -- CV and
    cover letter generation now happens AFTER you approve a job from this
    email, not before, so nothing gets tailored for a job you never asked
    for. Before that change, the tailored-resume requirement was *also* what
    kept non-Swiss jobs out of the digest, since tailoring only ever ran on
    region_quota.select_with_quota()'s Swiss-only selection -- that geo
    filter is applied explicitly here now, via the same select_with_quota(),
    so a UK/Ireland/France/US job can't resurface in the digest just because
    it still has an active profile.json region_rule for scoring purposes.

    digest_sent_at IS NULL is what guarantees a job is never emailed twice --
    once included in a digest it's stamped and permanently excluded from
    every future one, even across days. The discovered_at cutoff is a second,
    independent freshness guard on top of the same cutoff already applied at
    scoring (database.py get_jobs_by_stage) -- belt and suspenders, so a job
    can never reach an inbox once it's gone stale, regardless of how long it
    sat mid-pipeline.
    """
    rows = conn.execute(
        "SELECT * FROM jobs WHERE fit_score >= ? "
        "AND digest_sent_at IS NULL "
        "AND (apply_status IS NULL OR apply_status != 'applied') "
        "AND discovered_at >= datetime('now', '-4 days') "
        "AND (detail_error IS NULL OR detail_error != 'expired') "
        "ORDER BY fit_score DESC, discovered_at DESC",
        (min_score,),
    ).fetchall()
    if not rows:
        return []
    columns = rows[0].keys()
    candidates = [dict(zip(columns, row)) for row in rows]

    from applypilot.scoring.region_quota import select_with_quota
    shortlist = select_with_quota(candidates, limit)
    if not shortlist:
        return []

    # Re-visit each shortlisted posting before emailing it. Descriptions are
    # scraped once at discovery and can be days old by now: a posting that
    # closed in the meantime still reads as live, and closed roles were
    # reaching the inbox as "matches". Only the shortlist is checked (<= limit
    # URLs), so this costs seconds, not a crawl.
    try:
        from applypilot.enrichment.detail import recheck_jobs
        stats = recheck_jobs([j["url"] for j in shortlist])
        if stats.get("expired"):
            log.info("Digest: dropped %d shortlisted job(s) that are no longer open.",
                     stats["expired"])
            still_open = {
                r[0] for r in conn.execute(
                    "SELECT url FROM jobs WHERE url IN ({}) "
                    "AND (detail_error IS NULL OR detail_error != 'expired')".format(
                        ",".join("?" * len(shortlist))
                    ),
                    [j["url"] for j in shortlist],
                ).fetchall()
            }
            shortlist = [j for j in shortlist if j["url"] in still_open]
    except Exception as e:
        # Never let a freshness check stop the digest going out.
        log.warning("Digest: freshness recheck failed (%s) -- sending unchecked.", e)

    return shortlist


def _why_bullets(job: dict) -> list[str]:
    """Short bullet reasons this job matched.

    Prefers the structured `score_why` the scorer now emits. Falls back to
    splitting the older single-blob `score_reasoning` into sentences, so jobs
    scored before that field existed still render as bullets instead of
    showing nothing.
    """
    why = (job.get("score_why") or "").strip()
    if why:
        return [b.strip(" -\t") for b in why.split("\n") if b.strip(" -\t")]

    reasoning = (job.get("score_reasoning") or "").strip()
    if not reasoning:
        return []
    parts = reasoning.split("\n", 1)
    prose = (parts[1] if len(parts) > 1 else parts[0]).strip()
    if not prose:
        return []
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", prose) if x.strip()]
    return sentences[:4]


def _missing_text(job: dict) -> str:
    """One line naming the gap. Blank for jobs scored before the field existed."""
    return (job.get("score_missing") or "").strip() or "Not assessed"


def _job_link(job: dict) -> str:
    app_url = job.get("application_url")
    if app_url and str(app_url).strip().lower() in ("none", "null", "nan", ""):
        app_url = None
    return app_url or job["url"]


def _build_email_body(jobs: list[dict]) -> str:
    """Plain-text fallback part (for clients that don't render HTML)."""
    plural = "es" if len(jobs) != 1 else ""
    lines = [
        f"{len(jobs)} new job match{plural} found.",
        "",
        "Reply with the numbers you want to apply to (e.g. \"1, 4, 7\", or \"all\").",
        "ApplyPilot will tailor your CV and cover letter to each and submit it.",
        "",
        "=" * 60,
        "",
    ]
    for i, j in enumerate(jobs, start=1):
        lines.append(f"{i}) {j['title']} -- {j.get('company') or j.get('site', 'Unknown')}")
        lines.append(f"   Match: {j.get('fit_score', '?')}/10   {j.get('location', 'N/A')}")
        lines.append("   Why:")
        for b in _why_bullets(j) or ["(no reasoning captured)"]:
            lines.append(f"     - {b}")
        lines.append(f"   What's missing: {_missing_text(j)}")
        lines.append(f"   Link: {_job_link(j)}")
        lines.append("")
    return "\n".join(lines)


# Palette: soft lavender ground, white cards, periwinkle headings.
_C_GROUND = "#E7E6F7"
_C_CARD = "#FFFFFF"
_C_INK = "#2E2E45"
_C_MUTED = "#6F6F8C"
_C_ACCENT = "#4F51D8"
_C_PILL_BG = "#EDECFC"


def _build_email_body_html(jobs: list[dict]) -> str:
    plural = "es" if len(jobs) != 1 else ""
    P = []
    P.append(
        f'<div style="margin:0;padding:0;background-color:{_C_GROUND};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'
        f' style="background-color:{_C_GROUND};padding:28px 12px;">'
        f'<tr><td align="center">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'
        f' style="max-width:640px;width:100%;">'
    )

    # Header
    P.append(
        f'<tr><td style="padding:8px 12px 22px 12px;">'
        f'<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:34px;'
        f'font-weight:300;letter-spacing:-0.5px;color:{_C_ACCENT};line-height:1.15;">'
        f'{len(jobs)} new match{plural}</div>'
        f'<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;'
        f'color:{_C_MUTED};padding-top:8px;line-height:1.55;">'
        f'Reply with the numbers you want to apply to &mdash; e.g. '
        f'<span style="color:{_C_INK};font-weight:600;">1, 4, 7</span> or '
        f'<span style="color:{_C_INK};font-weight:600;">all</span>. '
        f'ApplyPilot tailors your CV and cover letter to each one and submits it.</div>'
        f'</td></tr>'
    )

    for i, j in enumerate(jobs, start=1):
        title = escape(str(j.get("title") or "Untitled"))
        company = escape(str(j.get("company") or j.get("site") or "Unknown"))
        score = j.get("fit_score", "?")
        location = escape(str(j.get("location") or "N/A"))
        link = escape(str(_job_link(j)), quote=True)
        missing = escape(_missing_text(j))
        bullets = _why_bullets(j)

        why_html = "".join(
            f'<li style="margin:0 0 5px 0;">{escape(b)}</li>' for b in bullets
        ) or '<li style="margin:0;">No reasoning captured</li>'

        P.append(
            f'<tr><td style="padding:0 0 14px 0;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'
            f' style="background-color:{_C_CARD};border-radius:16px;">'
            f'<tr><td style="padding:22px 24px;font-family:Segoe UI,Helvetica,Arial,sans-serif;">'

            # number + title
            f'<div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;'
            f'color:{_C_MUTED};padding-bottom:6px;">{i:02d}</div>'
            f'<div style="font-size:19px;font-weight:600;color:{_C_INK};line-height:1.3;">{title}</div>'
            f'<div style="font-size:14px;color:{_C_ACCENT};padding-top:3px;">{company}</div>'

            # meta row
            f'<div style="padding-top:12px;">'
            f'<span style="display:inline-block;background-color:{_C_PILL_BG};color:{_C_ACCENT};'
            f'font-size:13px;font-weight:600;padding:4px 11px;border-radius:20px;">{score}/10</span>'
            f'<span style="font-size:13px;color:{_C_MUTED};padding-left:10px;">{location}</span>'
            f'</div>'

            # why
            f'<div style="font-size:12px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;'
            f'color:{_C_MUTED};padding:16px 0 6px 0;">Why</div>'
            f'<ul style="margin:0;padding-left:18px;font-size:14px;color:{_C_INK};line-height:1.5;">'
            f'{why_html}</ul>'

            # missing
            f'<div style="font-size:12px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;'
            f'color:{_C_MUTED};padding:14px 0 5px 0;">What&rsquo;s missing</div>'
            f'<div style="font-size:14px;color:{_C_INK};line-height:1.5;">{missing}</div>'

            # link
            f'<div style="padding-top:16px;">'
            f'<a href="{link}" style="font-size:14px;color:{_C_ACCENT};text-decoration:none;'
            f'font-weight:600;">View posting &rsaquo;</a></div>'

            f'</td></tr></table></td></tr>'
        )

    P.append(
        f'<tr><td style="padding:6px 12px 0 12px;font-family:Segoe UI,Helvetica,Arial,sans-serif;'
        f'font-size:12px;color:{_C_MUTED};">ApplyPilot</div></td></tr>'
    )
    P.append('</table></td></tr></table></div>')
    return "".join(P)


def _last_digest_batch(conn) -> list[dict]:
    """The exact ordered job list from the most recently sent digest, so a
    reply like "apply to 1, 4, 7" maps back to the jobs those numbers were
    printed against.

    Ordered by the digest_position stamped when the email was built -- NOT
    re-derived. Re-deriving is what broke this before: the email numbers jobs
    in region-quota order (one bucket then the next), while the old
    reconstruction sorted by fit_score, so 16 of 20 positions disagreed and a
    reply would have approved the wrong jobs. The position is now recorded at
    send time and read back verbatim, so the two can never drift again.
    """
    last_sent = conn.execute("SELECT MAX(digest_sent_at) FROM jobs").fetchone()[0]
    if not last_sent:
        return []
    rows = conn.execute(
        "SELECT * FROM jobs WHERE digest_sent_at = ? AND digest_position IS NOT NULL "
        "ORDER BY digest_position",
        (last_sent,),
    ).fetchall()
    if not rows:
        return []
    columns = rows[0].keys()
    return [dict(zip(columns, row)) for row in rows]


def send_daily_digest(min_score: int = 7, limit: int = 20) -> dict:
    """Email a digest of ready-to-apply jobs. Marks included jobs so they
    aren't repeated in tomorrow's digest. Never touches apply_status --
    submitting is always a separate, manual step.

    Returns:
        {"sent": 0 or 1, "jobs": int, "error": str | None}
    """
    load_env()
    profile = load_profile()
    conn = get_connection()

    jobs = _pending_digest_jobs(conn, min_score, limit)
    if not jobs:
        log.info("Digest: no new ready-to-apply jobs since the last digest.")
        return {"sent": 0, "jobs": 0, "error": None}

    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_address or not gmail_app_password:
        log.error("Digest: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env -- cannot send.")
        return {"sent": 0, "jobs": len(jobs), "error": "missing email credentials"}

    recipient = profile.get("personal", {}).get("email") or gmail_address

    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = recipient
    plural = "es" if len(jobs) != 1 else ""
    msg["Subject"] = (
        f"ApplyPilot: {len(jobs)} job match{plural} ready for review "
        f"({datetime.now().strftime('%Y-%m-%d')})"
    )

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(_build_email_body(jobs), "plain"))
    alt.attach(MIMEText(_build_email_body_html(jobs), "html"))
    msg.attach(alt)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(gmail_address, gmail_app_password)
            server.send_message(msg)
    except Exception as e:
        log.error("Digest: failed to send email: %s", e)
        return {"sent": 0, "jobs": len(jobs), "error": str(e)}

    # Record the position each job was printed at, in the same order the email
    # numbered them. check_email_approvals reads this back verbatim, so a reply
    # can never map to a different job than the one you saw next to that number.
    now = datetime.now(timezone.utc).isoformat()
    for position, j in enumerate(jobs, start=1):
        conn.execute(
            "UPDATE jobs SET digest_sent_at = ?, digest_position = ? WHERE url = ?",
            (now, position, j["url"]),
        )
    conn.commit()

    log.info("Digest sent to %s: %d jobs", recipient, len(jobs))
    return {"sent": 1, "jobs": len(jobs), "error": None}


def send_apply_run_notification(
    captcha_jobs: list[dict], reviewed_jobs: list[dict], applied_jobs: list[dict],
) -> dict:
    """Email a summary right after an `applypilot apply` run, so CAPTCHA
    blocks and review-ready applications don't sit silently in the database
    until someone remembers to check.

    - captcha_jobs: blocked by an unsolvable CAPTCHA -- these need the user
      to go solve it by hand on the actual posting (CapSolver isn't
      configured, or it failed).
    - reviewed_jobs: a --dry-run pass filled the form and is waiting for
      --approve before any real submission (includes review_notes).
    - applied_jobs: a real (live) run actually submitted these.

    Sends nothing if all three lists are empty.

    Returns:
        {"sent": 0 or 1, "error": str | None}
    """
    if not (captcha_jobs or reviewed_jobs or applied_jobs):
        return {"sent": 0, "error": None}

    load_env()
    profile = load_profile()
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_address or not gmail_app_password:
        log.error("Apply notification: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set -- cannot send.")
        return {"sent": 0, "error": "missing email credentials"}

    recipient = profile.get("personal", {}).get("email") or gmail_address

    parts = []
    if captcha_jobs:
        parts.append(f"{len(captcha_jobs)} CAPTCHA")
    if reviewed_jobs:
        parts.append(f"{len(reviewed_jobs)} ready for review")
    if applied_jobs:
        parts.append(f"{len(applied_jobs)} applied")
    subject = f"ApplyPilot apply run: {', '.join(parts)} ({datetime.now().strftime('%Y-%m-%d %H:%M')})"

    lines: list[str] = []

    if captcha_jobs:
        lines.append(
            f"=== {len(captcha_jobs)} job(s) BLOCKED BY CAPTCHA -- please solve these yourself ==="
        )
        lines.append("These were not applied to. Open the link and apply manually.")
        lines.append("")
        for j in captcha_jobs:
            lines.append(f"[{j.get('fit_score', '?')}/10] {j['title']} -- {j.get('company') or j.get('site', 'Unknown')}")
            lines.append(f"  Link: {j.get('application_url') or j['url']}")
            lines.append("")

    if reviewed_jobs:
        lines.append(f"=== {len(reviewed_jobs)} job(s) filled out and ready for your review (not submitted) ===")
        lines.append("Read each summary, then reply to THIS email with the numbers you approve")
        lines.append("(e.g. \"1, 3\" or \"all\") to submit them for real on the next run. You can")
        lines.append("also use: applypilot apply --approve <url>  (or --approve-all / --list-pending)")
        lines.append("")
        for i, j in enumerate(reviewed_jobs, start=1):
            lines.append(f"{i}) [{j.get('fit_score', '?')}/10] {j['title']} -- {j.get('company') or j.get('site', 'Unknown')}")
            lines.append(f"   Link: {j['url']}")
            notes = (j.get("review_notes") or "").strip()
            if notes:
                lines.append(f"   Summary: {notes[:500]}")
            lines.append("   Approve for real submission?: yes / no")
            lines.append("")

    if applied_jobs:
        lines.append(f"=== {len(applied_jobs)} job(s) actually submitted ===")
        lines.append("")
        for j in applied_jobs:
            lines.append(f"[{j.get('fit_score', '?')}/10] {j['title']} -- {j.get('company') or j.get('site', 'Unknown')}")
            lines.append(f"  Link: {j.get('application_url') or j['url']}")
            lines.append("")

    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText("\n".join(lines), "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(gmail_address, gmail_app_password)
            server.send_message(msg)
    except Exception as e:
        log.error("Apply notification: failed to send email: %s", e)
        return {"sent": 0, "error": str(e)}

    log.info("Apply notification sent to %s: %d captcha, %d reviewed, %d applied",
              recipient, len(captcha_jobs), len(reviewed_jobs), len(applied_jobs))
    return {"sent": 1, "error": None}


# ── Reply-based approval (reading, not sending) ───────────────────────────
#
# Two-stage flow, both driven by replying to an email -- no CLI needed:
#   1. Reply to the DIGEST ("Re: ApplyPilot: N job match...") with numbers
#      -> those jobs get apply_status='requested', which the next apply
#      review pass (--dry-run) prioritizes so the form actually gets filled
#      and a preview emailed, before anything is ever submitted.
#   2. Reply to the APPLY-RUN NOTIFICATION ("Re: ApplyPilot apply run: ...")
#      with numbers -> those jobs (from the pending_review list) get
#      promoted to apply_status='approved' via the same approve_job() the
#      CLI --approve flag uses, making them eligible for the next
#      live-submit stage.
# Never skips a stage -- a digest reply alone cannot make a job submittable,
# only reviewable. That's the point: no submission without seeing the
# filled-out form first.

def _reconstruct_pending_review_batch(conn) -> list[dict]:
    """Same fit_score DESC ordering send_apply_run_notification's
    reviewed_jobs list uses, so a reply's numbers map back to the right
    jobs -- reconstructed fresh each time rather than tied to one specific
    email, since pending_review is a stable queue until approved."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE apply_status = 'pending_review' ORDER BY fit_score DESC"
    ).fetchall()
    if not rows:
        return []
    columns = rows[0].keys()
    return [dict(zip(columns, row)) for row in rows]


_SIGNATURE_MARKERS = (
    "[image:",          # Gmail's inline-image placeholder, where signatures start
    "\n-- \n",          # RFC 3676 signature delimiter
)


def _parse_reply_numbers(body: str) -> tuple[set[int], bool]:
    """Extract the job numbers you approved (and whether you said "all") from
    a reply body.

    Only your own new text counts. Everything below it is discarded: the
    quoted original (Gmail marks it with "On ... wrote:" and "> " lines) and
    your signature block. Then URLs are stripped and only standalone numbers
    are taken.

    All three of those matter. A real reply -- "apply to 3,5,8,10, 15, 17,
    20" -- carried an email signature containing
    "calendly.com/hercode-ch/30min", and a bare \\d+ scan pulled "30" out of
    that URL and treated it as an approved job number. It happened to be out
    of range that time; a signature with a phone extension or a "15% off"
    footer would have silently approved a job that was never named.
    """
    reply_text = re.split(r"\n\s*On .+ wrote:", body, maxsplit=1)[0]
    reply_text = "\n".join(
        line for line in reply_text.splitlines() if not line.strip().startswith(">")
    )
    # Cut the signature block off before any digits are read.
    for marker in _SIGNATURE_MARKERS:
        idx = reply_text.find(marker)
        if idx != -1:
            reply_text = reply_text[:idx]
    # Drop URLs and e-mail addresses wholesale -- they are dense with digits
    # that are never job numbers.
    reply_text = re.sub(r"https?://\S+|www\.\S+|\S+@\S+", " ", reply_text)

    approve_all = bool(re.search(r"\ball\b", reply_text, re.IGNORECASE))
    # Standalone 1-3 digit runs only: "30min" and "v2" no longer qualify,
    # because a digit run touching a letter or a slash is not a job number.
    numbers = {
        int(n) for n in re.findall(r"(?<![\w/])\d{1,3}(?![\w/])", reply_text)
    }
    return numbers, approve_all


def _extract_plain_text(msg) -> str:
    """Pull the text/plain body out of an email.message.Message, multipart or not."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


def _decode_subject(msg) -> str:
    """The Subject header as plain text, RFC 2047 encoded words decoded."""
    raw = msg.get("Subject", "")
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw or ""


def _state_path():
    from applypilot.config import APP_DIR
    return APP_DIR / "imap_state.json"


def _load_imap_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_imap_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        log.error("Could not persist IMAP state: %s", e)


def _fetch_new_replies(imap: imaplib.IMAP4_SSL, subject_substring: str,
                       state_key: str, state: dict) -> list[str]:
    """Return the bodies of replies not yet processed, tracked by IMAP UID.

    Deliberately does NOT filter on \\Seen. The digest is sent from the same
    Gmail account it is delivered to, so a reply is addressed to yourself --
    and Gmail marks your own outgoing mail as read the moment you send it. An
    UNSEEN search therefore matched nothing, ever: replies sat in the inbox
    while every poll reported "nothing approved". Verified against the live
    mailbox -- 2 matching replies present, 0 of them UNSEEN.

    UIDs are monotonic within a mailbox, so remembering the highest one
    processed is enough to pick up only what is new, regardless of read
    state. UIDVALIDITY is stored alongside it: if the server ever renumbers
    the mailbox, the watermark is dropped rather than trusted.
    """
    typ, data = imap.status("INBOX", "(UIDVALIDITY)")
    uidvalidity = None
    if typ == "OK" and data and data[0]:
        found = re.search(rb"UIDVALIDITY (\d+)", data[0])
        if found:
            uidvalidity = found.group(1).decode()

    entry = state.get(state_key) or {}
    last_uid = entry.get("last_uid", 0)
    if entry.get("uidvalidity") != uidvalidity:
        last_uid = 0  # mailbox renumbered (or first run) -- watermark is meaningless

    typ, data = imap.uid("SEARCH", None, "SUBJECT", f'"{subject_substring}"')
    if typ != "OK" or not data or not data[0]:
        state[state_key] = {"uidvalidity": uidvalidity, "last_uid": last_uid}
        return []

    uids = sorted(int(u) for u in data[0].split())
    new_uids = [u for u in uids if u > last_uid]

    bodies = []
    for uid in new_uids:
        typ, msg_data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = email_lib.message_from_bytes(msg_data[0][1])
        # Re-check the Subject ourselves. Gmail's IMAP SUBJECT search matches
        # on words, not on the literal string, so a search for
        # "Re: ApplyPilot: " also returns "Re: ApplyPilot apply run: ..." --
        # they share the tokens "re" and "applypilot". That crossover let a
        # reply meant for one email be counted against the other's numbered
        # list, which approved a job the reply never named. The server search
        # is now only a coarse prefilter; this substring check is what decides.
        if subject_substring.lower() not in _decode_subject(msg).lower():
            continue
        bodies.append(_extract_plain_text(msg))

    state[state_key] = {
        "uidvalidity": uidvalidity,
        "last_uid": max(uids) if uids else last_uid,
    }
    return bodies


def check_email_approvals() -> dict:
    """Check for replies to the digest and apply-run-notification emails
    and advance the affected jobs one step each (see module notes above).

    Uses IMAP with the same GMAIL_ADDRESS/GMAIL_APP_PASSWORD already
    configured for sending -- no new credentials needed. Best-effort: any
    failure here should never block the rest of the daily pipeline.

    Returns:
        {"requested": list[str] (urls), "approved": list[str] (urls), "error": str | None}
    """
    load_env()
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_address or not gmail_app_password:
        return {"requested": [], "approved": [], "error": "missing email credentials"}

    conn = get_connection()

    state = _load_imap_state()
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(gmail_address, gmail_app_password)
        imap.select("INBOX")
        digest_bodies = _fetch_new_replies(imap, "Re: ApplyPilot: ", "digest", state)
        review_bodies = _fetch_new_replies(imap, "Re: ApplyPilot apply run", "review", state)
        imap.logout()
    except Exception as e:
        log.error("check_email_approvals: IMAP error: %s", e)
        return {"requested": [], "approved": [], "error": str(e)}
    # Only advance the watermark once the mailbox read succeeded, so a network
    # failure re-reads rather than silently skipping a reply.
    _save_imap_state(state)

    requested_urls: list[str] = []
    if digest_bodies:
        batch = _last_digest_batch(conn)
        numbers: set[int] = set()
        approve_all = False
        for body in digest_bodies:
            nums, all_flag = _parse_reply_numbers(body)
            numbers |= nums
            approve_all = approve_all or all_flag
        for i, job in enumerate(batch, start=1):
            if approve_all or i in numbers:
                # Single-gate flow: replying to the digest with a job's number
                # IS the approval to apply. It goes straight to 'approved',
                # which the live-submit stage draws from -- there is no second
                # confirmation email. Nothing you didn't name by number is ever
                # touched, and the job still can't be submitted until a CV and
                # cover letter have been tailored for it (acquire_job requires
                # tailored_resume_path), which daily() does first.
                conn.execute(
                    "UPDATE jobs SET apply_status = 'approved' WHERE url = ? "
                    "AND (apply_status IS NULL OR apply_status = 'failed')",
                    (job["url"],),
                )
                requested_urls.append(job["url"])
        conn.commit()

    approved_urls: list[str] = []
    if review_bodies:
        from applypilot.apply.launcher import approve_job
        batch = _reconstruct_pending_review_batch(conn)
        numbers = set()
        approve_all = False
        for body in review_bodies:
            nums, all_flag = _parse_reply_numbers(body)
            numbers |= nums
            approve_all = approve_all or all_flag
        for i, job in enumerate(batch, start=1):
            if approve_all or i in numbers:
                if approve_job(job["url"]):
                    approved_urls.append(job["url"])

    if requested_urls or approved_urls:
        log.info(
            "check_email_approvals: %d requested for review, %d approved for submission",
            len(requested_urls), len(approved_urls),
        )

    return {"requested": requested_urls, "approved": approved_urls, "error": None}

