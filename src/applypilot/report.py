"""Daily application report: what ApplyPilot applied to today, for email/logging."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from applypilot.database import get_connection

log = logging.getLogger(__name__)

_REPORT_COLUMNS = (
    "title", "site", "location", "fit_score", "application_url",
    "tailored_resume_path", "cover_letter_path", "applied_at", "apply_status",
)


def get_applications_since(since_iso: str) -> list[dict]:
    """Return all jobs applied to at or after the given ISO timestamp, most recent first."""
    conn = get_connection()
    rows = conn.execute(
        f"SELECT {', '.join(_REPORT_COLUMNS)} FROM jobs "
        "WHERE applied_at IS NOT NULL AND applied_at >= ? "
        "ORDER BY applied_at DESC",
        (since_iso,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_todays_applications() -> list[dict]:
    """Return all jobs applied to today (UTC calendar day), most recent first."""
    conn = get_connection()
    today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        f"SELECT {', '.join(_REPORT_COLUMNS)} FROM jobs "
        "WHERE applied_at IS NOT NULL AND date(applied_at) = ? "
        "ORDER BY applied_at DESC",
        (today,),
    ).fetchall()
    return [dict(row) for row in rows]


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_report_html(applications: list[dict]) -> str:
    """Render a daily application report as a self-contained HTML email body."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not applications:
        return (
            f"<h2>ApplyPilot Daily Report -- {date_str}</h2>"
            "<p>No applications were submitted today.</p>"
        )

    rows_html = ""
    for a in applications:
        job_link = (
            f'<a href="{_escape(a["application_url"])}">Open job</a>'
            if a.get("application_url") else "N/A"
        )
        status = a.get("apply_status") or "submitted"
        rows_html += f"""
        <tr>
          <td>{_escape(a.get("title", ""))}</td>
          <td>{_escape(a.get("site", ""))}</td>
          <td>{_escape(a.get("location", "") or "")}</td>
          <td style="text-align:center">{a.get("fit_score", "")}</td>
          <td>{job_link}</td>
          <td style="font-family:monospace;font-size:11px">{_escape(a.get("tailored_resume_path", "") or "")}</td>
          <td style="font-family:monospace;font-size:11px">{_escape(a.get("cover_letter_path", "") or "N/A")}</td>
          <td>{_escape(status)}</td>
        </tr>"""

    return f"""
    <h2>ApplyPilot Daily Report -- {date_str}</h2>
    <p>{len(applications)} application(s) submitted today.</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#f0f0f0">
        <th>Role</th><th>Company/Site</th><th>Location</th><th>Fit Score</th>
        <th>Job Link</th><th>Resume Used</th><th>Cover Letter Used</th><th>Status</th>
      </tr>
      {rows_html}
    </table>
    """


def build_report_text(applications: list[dict]) -> str:
    """Plain-text fallback version of the report."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not applications:
        return f"ApplyPilot Daily Report -- {date_str}\n\nNo applications were submitted today."

    lines = [f"ApplyPilot Daily Report -- {date_str}", f"{len(applications)} application(s) submitted today.", ""]
    for a in applications:
        lines.append(f"- {a.get('title', '')} @ {a.get('site', '')} ({a.get('location', '') or 'N/A'}) -- fit score {a.get('fit_score', '')}")
        lines.append(f"  Job: {a.get('application_url', 'N/A')}")
        lines.append(f"  Resume: {a.get('tailored_resume_path', '')}")
        lines.append(f"  Cover letter: {a.get('cover_letter_path', 'N/A')}")
        lines.append("")
    return "\n".join(lines)
