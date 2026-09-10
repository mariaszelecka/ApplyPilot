"""Appends a row to your own manual job-search tracker spreadsheet after
ApplyPilot submits a real application.

Optional. Set APPLYPILOT_TRACKER_XLSX to the full path of an .xlsx file to
enable it; if unset, this module does nothing. The workbook is expected to
have one tab per month (named by month, full or 3-letter) with columns
A=Date B=Firm C=Role D=Link E=Location F=source-tag.

Best-effort and must never break the apply pipeline: any failure here is
logged and swallowed, never raised, and the sheet is written defensively --
see _is_file_locked below for why.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _tracker_path() -> Path | None:
    raw = os.environ.get("APPLYPILOT_TRACKER_XLSX", "").strip()
    return Path(raw) if raw else None


# Matched case-insensitively against a target month's full name or 3-letter
# abbreviation -- never auto-created, since guessing at a new month's
# formatting/layout risks doing it wrong. See log_application().
_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November",
    12: "December",
}

_SOURCE_TAG = "Applypilot"


def _is_file_locked(path: Path) -> bool:
    """True if Excel (or anything else) currently has the file open.

    Office apps drop a hidden `~$<name>` lock file next to a workbook while
    it's open. Writing to the real file at the same time is a real risk: the
    write can be rejected outright, or silently discarded the next time Excel
    saves, overwriting the disk copy with whatever is still in Excel's memory
    -- the added row would just vanish with no error. A file left open in
    Excel is a normal scenario this has to handle, not an edge case.
    """
    lock_path = path.with_name(f"~${path.name}")
    return lock_path.exists()


def _sheet_for_month(sheetnames: list[str], dt: datetime) -> str | None:
    full = _MONTH_NAMES[dt.month].lower()
    abbr = full[:3]
    for name in sheetnames:
        low = name.strip().lower()
        if low == full or low == abbr:
            return name
    return None


def _next_row(ws, cols: str = "ABCDEF") -> int:
    """First row (after the header) with every one of `cols` empty."""
    row = 2
    while any(ws[f"{c}{row}"].value not in (None, "") for c in cols):
        row += 1
    return row


def log_application(url: str) -> bool:
    """Append one row for a job that was just successfully submitted.

    Args:
        url: The job's canonical URL (jobs.url), used to look up its details.

    Returns:
        True if a row was written, False if it was skipped (file locked,
        month tab not found, file missing, or any other failure) -- callers
        should treat False as "best-effort, no row added" and move on.
    """
    tracker_path = _tracker_path()
    if tracker_path is None:
        return False  # feature not configured -- silent no-op

    try:
        from applypilot.database import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT title, company, location, url, application_url "
            "FROM jobs WHERE url = ?",
            (url,),
        ).fetchone()
        if not row:
            log.warning("tracker_sheet: no DB row for %s -- skipping.", url)
            return False
        job = dict(row)

        if not tracker_path.exists():
            log.warning("tracker_sheet: %s not found -- skipping.", tracker_path)
            return False

        if _is_file_locked(tracker_path):
            log.warning(
                "tracker_sheet: %s is open in Excel -- skipping to avoid a "
                "write that could be silently lost on next Save. Add this "
                "one manually: %s @ %s",
                tracker_path.name, job.get("title"), job.get("company"),
            )
            return False

        import openpyxl
        now = datetime.now(timezone.utc)
        wb = openpyxl.load_workbook(tracker_path)
        sheet_name = _sheet_for_month(wb.sheetnames, now)
        if not sheet_name:
            log.warning(
                "tracker_sheet: no tab found for %s among %s -- skipping "
                "rather than guessing a new tab's layout.",
                _MONTH_NAMES[now.month], wb.sheetnames,
            )
            return False
        ws = wb[sheet_name]

        r = _next_row(ws)
        link = job.get("application_url") or job.get("url")
        title = (job.get("title") or "").strip()

        ws[f"A{r}"] = now.strftime("%d.%m.%Y")
        ws[f"B{r}"] = job.get("company") or ""
        title_cell = ws[f"C{r}"]
        title_cell.value = title
        if link:
            title_cell.hyperlink = link
        ws[f"D{r}"] = link or ""
        if job.get("location"):
            ws[f"E{r}"] = job["location"]
        tag_cell = ws[f"F{r}"]
        tag_cell.value = _SOURCE_TAG
        tag_cell.font = tag_cell.font.copy(bold=True)

        wb.save(tracker_path)
        log.info("tracker_sheet: logged '%s' @ '%s' to %s!%s (row %d)",
                 title, job.get("company"), sheet_name, "!", r)
        return True

    except Exception as e:
        # Never let a spreadsheet problem take down a real apply run.
        log.warning("tracker_sheet: failed to log %s: %s", url, e)
        return False
